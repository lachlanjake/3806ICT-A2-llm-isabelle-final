# 3806ICT Assignment 2 - LLM Isabelle Integration

This repository contains the final integrated implementation and benchmark evidence for the 3806ICT Assignment 2 LLM-Isabelle project.

## System Overview

The final system connects three main components:

- Part 1: stepwise prover and Isabelle interaction
- Part 2: planner / Isar proof route
- Part 3: fill and CEGIS-style repair support

The main integrated route is:

planner.cli -> planner.driver -> planner.repair -> prover.cli -> Isabelle verification

## Running a Single Integrated Proof

Example command:

```bash
python -m planner.cli --goal "rev (rev xs) = xs" --model qwen2.5:3b --timeout 120 --trace
```

## Running the Benchmark

Example command:

```bash
python run_tests.py final_integrated datasets/final_manifest.csv
```

The benchmark records:

- expected result
- actual result
- correct behaviour
- runtime in seconds

## Datasets

The benchmark uses three datasets.

### Dataset 1: `dataset1_logic_course.txt`

This dataset contains course/textbook-style propositional and first-order logic goals. It includes examples involving implication, conjunction, disjunction, negation, and quantifier-style reasoning.

### Dataset 2: `dataset2_isabelle_main_library.txt`

This dataset contains Isabelle/HOL Main-style list and natural-number goals. It includes common theorem-proving examples such as list reversal, list length, append, map, filtering, and simple natural-number arithmetic.

### Dataset 3: `dataset3_robustness_negative_controls.txt`

This dataset contains robustness and negative-control goals. It includes both valid stress-test goals and intentionally invalid goals. The invalid goals are included to check that the system does not incorrectly accept false statements as proved.

### Manifest File

`final_manifest.csv` combines the three datasets and records whether each goal is expected to be valid or invalid.

## Final Benchmark Results

### Final Integrated System

- Total goals: 72
- Valid goals proved: 61/61
- Invalid goals correctly rejected: 11/11
- Correct behaviour: 72/72
- Average runtime: 26.355 seconds

### Baseline Repository

- Total goals: 72
- Valid goals proved: 0/61
- Invalid goals correctly rejected: 11/11
- Correct behaviour: 11/72
- Average runtime: 21.167 seconds

The baseline was faster on average because it failed quickly on the valid goals. The final integrated system took slightly longer, but produced verified proofs for all valid benchmark goals and correctly rejected all invalid benchmark goals.

## Evidence Files

Datasets are stored in:

"datasets"

Result summaries and CSV files are stored in:

"results"

Logs are stored in:

"logs"


Screenshots and final evidence are stored in:

"final_evidence"

## Main Result Summary

The final integrated system improved the original baseline by connecting the planner, repair module, stepwise prover, and Isabelle verification route. The final system achieved 100% correct behaviour across the 72-goal benchmark, while the original baseline achieved 15.28% correct behaviour on the same benchmark.