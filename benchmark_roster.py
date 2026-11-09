"""One live end-to-end run, with observed latency and cost (no Discord messages)."""
import argparse
import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
from backend.schedule_service import analyze_schedule_image


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected", type=Path)
    args = parser.parse_args()
    result = await analyze_schedule_image(args.image.read_bytes(), filename=args.image.name,
                                          content_type="image/jpeg", all_employees=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"usage": result["usage"], "timing": result["timing"]}, ensure_ascii=False))
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        differences = []
        for employee in result["employees"]:
            code = employee["employee_code"]
            wanted = expected["employees"][code]
            actual = {a["time"]: a["shift"] for a in employee["assignments"]}
            for slot in sorted(wanted.keys() | actual.keys()):
                if wanted.get(slot) != actual.get(slot):
                    differences.append({"code": code, "time": slot, "expected": wanted.get(slot), "actual": actual.get(slot)})
            if employee["document_date"] != expected["document_date"]:
                differences.append({"code": code, "error": "date mismatch"})
            if len(actual) != len(employee["assignments"]):
                differences.append({"code": code, "error": "duplicate time"})
        print(json.dumps({"differences": differences, "passed": not differences}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
