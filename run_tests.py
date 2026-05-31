import csv
import subprocess
import time
import sys
from pathlib import Path
from collections import defaultdict

system_name = sys.argv[1] if len(sys.argv) > 1 else "final_integrated"
manifest_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("datasets/final_manifest.csv")

Path("logs").mkdir(exist_ok=True)
Path("results").mkdir(exist_ok=True)

results_path = Path(f"results/{system_name}_results.csv")
log_path = Path(f"logs/{system_name}_log.txt")
summary_path = Path(f"results/{system_name}_summary.txt")

rows = list(csv.DictReader(manifest_path.open()))

with results_path.open("w", newline="", encoding="utf-8") as out, log_path.open("w", encoding="utf-8") as log:
    writer = csv.writer(out)
    writer.writerow([
        "system", "dataset", "expected", "goal",
        "exit_code", "actual", "correct_behaviour", "runtime_sec"
    ])

    for row in rows:
        dataset = row["dataset"]
        expected = row["expected"]
        goal = row["goal"]

        print("=" * 60)
        print(f"DATASET: {dataset}")
        print(f"GOAL: {goal}")

        log.write("=" * 70 + "\n")
        log.write(f"SYSTEM: {system_name}\n")
        log.write(f"DATASET: {dataset}\n")
        log.write(f"EXPECTED: {expected}\n")
        log.write(f"GOAL: {goal}\n\n")
        log.flush()

        cmd = [
            sys.executable, "-m", "planner.cli",
            "--goal", goal,
            "--model", "qwen2.5:3b",
            "--timeout", "60",
            "--trace",
        ]

        start = time.time()

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=90,
            )
            exit_code = proc.returncode
            output = proc.stdout
        except subprocess.TimeoutExpired as e:
            exit_code = 124
            output = (e.stdout or "") + "\nTIMEOUT: subprocess exceeded 90 seconds\n"

        runtime = round(time.time() - start, 3)

        actual = "proved" if exit_code == 0 else "failed_or_unproved"

        if expected == "valid" and actual == "proved":
            correct = "yes"
        elif expected == "invalid" and actual == "failed_or_unproved":
            correct = "yes"
        else:
            correct = "no"

        print(f"RESULT: {actual}")
        print(f"EXPECTED: {expected}")
        print(f"CORRECT: {correct}")
        print(f"RUNTIME: {runtime} sec")

        log.write(output)
        log.write(f"\nACTUAL: {actual}\n")
        log.write(f"CORRECT: {correct}\n")
        log.write(f"RUNTIME_SEC: {runtime}\n\n")
        log.flush()

        writer.writerow([
            system_name, dataset, expected, goal,
            exit_code, actual, correct, runtime
        ])

# Summary
data = list(csv.DictReader(results_path.open()))
by_dataset = defaultdict(list)

for r in data:
    by_dataset[r["dataset"]].append(r)

lines = []
lines.append(f"SUMMARY FOR {system_name}")
lines.append("=" * 60)

total = len(data)
proved = sum(1 for r in data if r["actual"] == "proved")
correct = sum(1 for r in data if r["correct_behaviour"] == "yes")
valid_total = sum(1 for r in data if r["expected"] == "valid")
valid_proved = sum(1 for r in data if r["expected"] == "valid" and r["actual"] == "proved")
invalid_total = sum(1 for r in data if r["expected"] == "invalid")
invalid_rejected = sum(1 for r in data if r["expected"] == "invalid" and r["actual"] == "failed_or_unproved")
avg_runtime = sum(float(r["runtime_sec"]) for r in data) / total if total else 0

lines.append(f"Total goals: {total}")
lines.append(f"Proved: {proved}")
lines.append(f"Correct behaviour: {correct}/{total} ({correct/total*100:.2f}%)")
lines.append(f"Valid goals proved: {valid_proved}/{valid_total}")
lines.append(f"Invalid goals correctly rejected: {invalid_rejected}/{invalid_total}")
lines.append(f"Average runtime: {avg_runtime:.3f} sec")
lines.append("")
lines.append("PER DATASET")
lines.append("-" * 60)

for dataset, rs in by_dataset.items():
    d_total = len(rs)
    d_proved = sum(1 for r in rs if r["actual"] == "proved")
    d_correct = sum(1 for r in rs if r["correct_behaviour"] == "yes")
    d_avg = sum(float(r["runtime_sec"]) for r in rs) / d_total if d_total else 0
    lines.append(
        f"{dataset}: proved={d_proved}/{d_total}, "
        f"correct={d_correct}/{d_total}, avg_runtime={d_avg:.3f}s"
    )

summary = "\n".join(lines)
summary_path.write_text(summary, encoding="utf-8")

print("=" * 60)
print(summary)
print("=" * 60)
print(f"Saved results to: {results_path}")
print(f"Saved log to: {log_path}")
print(f"Saved summary to: {summary_path}")
