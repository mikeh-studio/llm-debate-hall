# Legacy Test Notes

The executable, versioned evaluation set now lives in `evals/questions_v1.json` and is run with:

```bash
python3 scripts/run_evaluation.py --preset mock --model mock-model --limit 2
```

Use `--preset`, `--model`, `--judge-preset`, and `--judge-model` to run real-provider comparisons. Generated evidence is written under `artifacts/evaluations/` and is intentionally ignored by Git.
