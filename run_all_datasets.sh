#!/bin/bash

mkdir -p logs results
OUT="results/final_integrated_results.csv"
LOG="logs/final_integrated_run.txt"

echo "dataset,goal,exit_code,result" > "$OUT"
echo "" > "$LOG"

for file in datasets/dataset1_logic_course.txt datasets/dataset2_isabelle_main_library.txt datasets/dataset3_robustness_negative_controls.txt
do
  dataset=$(basename "$file" .txt)

  while IFS= read -r goal
  do
    echo "========================================" | tee -a "$LOG"
    echo "DATASET: $dataset" | tee -a "$LOG"
    echo "GOAL: $goal" | tee -a "$LOG"

    python -m planner.cli --goal "$goal" --model qwen2.5:3b --timeout 60 --trace >> "$LOG" 2>&1
    status=$?

    if [ $status -eq 0 ]; then
      result="proved"
    else
      result="failed_or_unproved"
    fi

    echo "\"$dataset\",\"$goal\",$status,$result" >> "$OUT"
    echo "RESULT: $result" | tee -a "$LOG"

  done < "$file"
done

echo "Done. Results saved to $OUT"
echo "Full log saved to $LOG"
