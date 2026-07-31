"""PoC-07: Rate limit 실측.

검증 항목
- X-RateLimit-Burst-Capacity / -Replenish-Rate 실측값
- Remaining 감소 곡선, 429 발생 지점, 429 응답 body와 Retry-After 헤더 유무
- 429 후 회복 시간

주의: 의도적으로 429를 유발하므로 반드시 업무 시간 외에 실행할 것.
      실행하려면 --yes 플래그 필요.

실행: python poc_07_ratelimit.py --yes [--max-requests 200] [--interval 0.05]
(선행: poc_01)
"""
import argparse
import sys
import time

from poc_common import PocClient

ap = argparse.ArgumentParser()
ap.add_argument("--yes", action="store_true", help="업무 시간 외 실행임을 확인")
ap.add_argument("--max-requests", type=int, default=200)
ap.add_argument("--interval", type=float, default=0.05)
args = ap.parse_args()

if not args.yes:
    print("이 스크립트는 의도적으로 429를 유발합니다. 업무 시간 외에 --yes 플래그와 함께 실행하세요.")
    sys.exit(2)

pc = PocClient("07_ratelimit")
try:
    curve = []
    hit_429 = None
    pc.log(f"연사 시작: 최대 {args.max_requests}회, 간격 {args.interval}s (429 재시도 없음)")
    for i in range(args.max_requests):
        t0 = time.monotonic()
        resp = pc.request("GET", "/drive/v1/drives", params={"type": "private"}, retry429=False, label=f"burst-{i}")
        sample = pc.ratelimit_samples[-1]
        curve.append({"i": i, "status": resp.status_code, "remaining": sample["remaining"], "elapsed_ms": round((time.monotonic() - t0) * 1000)})
        if resp.status_code == 429:
            hit_429 = {
                "at_request": i,
                "retry_after_header": resp.headers.get("Retry-After"),
                "body": resp.text[:500],
                "headers": {k: v for k, v in resp.headers.items() if "ratelimit" in k.lower() or k.lower() == "retry-after"},
            }
            pc.log(f"429 발생: {i}번째 요청, Retry-After={hit_429['retry_after_header']}")
            break
        time.sleep(args.interval)

    pc.results["curve"] = curve
    pc.results["hit_429"] = hit_429
    first = curve[0] if curve else {}
    pc.results["observed_burst_capacity"] = (pc.ratelimit_samples[0] or {}).get("burst") if pc.ratelimit_samples else None
    pc.results["observed_replenish_rate"] = (pc.ratelimit_samples[0] or {}).get("replenish") if pc.ratelimit_samples else None
    pc.log(f"실측: burst={pc.results['observed_burst_capacity']} replenish={pc.results['observed_replenish_rate']}/s")

    # 회복 시간 측정
    if hit_429:
        t0 = time.monotonic()
        while True:
            time.sleep(1)
            resp = pc.request("GET", "/drive/v1/drives", params={"type": "private"}, retry429=False, label="recovery-probe")
            if resp.status_code == 200:
                recovery = round(time.monotonic() - t0, 1)
                pc.results["recovery_seconds"] = recovery
                pc.log(f"회복 완료: {recovery}s")
                break
            if time.monotonic() - t0 > 120:
                pc.results["recovery_seconds"] = ">120"
                pc.log("120초 내 회복 안 됨")
                break
    else:
        pc.log(f"{args.max_requests}회 연사에도 429 미발생 — 여유 있는 정책이거나 요청 간격이 replenish보다 느림")

    pc.save()
finally:
    pc.close()
