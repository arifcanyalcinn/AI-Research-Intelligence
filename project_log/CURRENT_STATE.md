# Current Project State

Current Phase:
SDS Phases 0–3 complete (Phase 1 closed by §8.4). Next: Phase 4, Batch 11.

Completed Batches

[x] Batch 1
[x] Batch 2
[x] Batch 3
[x] Batch 4
[x] Batch 5
[DEFERRED] Batch 6
[x] Batch 7
[x] Batch 8
[x] Batch 9
[x] Batch 10 (ranking calibration, reduced scope)

Implemented Sources

[x] ArXiv
[x] HuggingFace Papers
[x] HuggingFace Models
[x] HuggingFace Spaces
[x] GitHub Trending
[DEFERRED] PapersWithCode

Current Test Count

750 Passed
672 Passed, 2 Skipped without the [embedding] extra

Gate (Decision 004 + D-007): python -m pytest, ruff check ., ruff format --check ., python -m arip check-config

Architecture Status

- Frozen SDS compliant
- No known architectural violations

Known Compatibility Issues

- respx 0.21.1 issue #277
- Temporary workaround: url__startswith
- Remove after upgrading to respx >= 0.22

Environment Notes

- Windows Smart App Control is OFF since 2026-11-16 (turned off by the user). It is the
  feature earlier recorded here as "Windows App Control". While on, it blocked
  pip-generated launcher .exe files (pytest.exe and similar) and, on 2026-11-16, unsigned
  native libraries: usearch's compiled .pyd in both venvs (it had still loaded in .venv on
  2026-11-14) and libbitsandbytes_cuda126.dll in the Batch 11 venv (WinError 4551). With it
  on, neither venv could run the test suite; after turning it off, both pass (750).
  Project rule unchanged: every command is written as python -m ... (python -m pytest,
  python -m alembic, python -m ruff).
- Two virtual environments, both editable installs of this checkout (git decides the code,
  the venv decides the libraries):
  - .venv (inside the repository): the demo environment. torch 2.14.0+cpu,
    transformers 5.16.1. Never modified in Batch 11. Used for the vize demo
    (2026-11-27) and for everything that does not load the LLM.
  - C:\Users\ASUS\venvs\arip-llm (outside the repository; Python 3.11.9): the Batch 11
    environment. torch 2.14.1+cu126 (CUDA 12.6), transformers 5.20.2, bitsandbytes 0.50.2,
    accelerate 1.15.0. Used for anything that loads the LLM. To rebuild it: install CUDA
    torch first (python -m pip install torch==2.14.1 --index-url
    https://download.pytorch.org/whl/cu126), then python -m pip install -e
    ".[dev,embedding,llm]". Never use --upgrade there.
- The Hugging Face cache did not survive the machine's service; the embedding model was
  re-downloaded in Batch 10. The Phase 4 model, Qwen/Qwen2.5-3B-Instruct (about 6.2 GB),
  is in the Hugging Face cache since 2026-11-16; check part 2 loaded it with the Hub
  offline.
- The pre-Batch-10 database and ANN index are archived in
  %USERPROFILE%\arip-archive\batch10-before-2026-10-28. The live data\arip.db is the fresh
  Batch 10 pool (250 rows, one run, scored under SDS §5.5.2).


Target Hardware

- GPU: NVIDIA GeForce RTX 4070 Laptop, 8188 MiB (8 GB) VRAM
- Ada Lovelace (CC 8.9) — 4-bit quantization kernels are supported
- Driver 621.01, CUDA UMD 13.5, WDDM, 80 W power cap
- Idle VRAM used by the Windows desktop and open apps: ~800 MiB,
  rising to ~1.5-2 GB in normal use. Effective LLM budget: ~6-6.5 GB.

- **SDS §7.1 correction.** §7.1 reconciles its 12 GB / 8 GB discrepancy by
  stating "The RTX 4070 has 12 GB VRAM" and instructing "design for 12 GB".
  That is the desktop RTX 4070. The target machine is the laptop variant at
  8 GB, so 8 GB is the actual budget, not a conservative floor. §7.1's own
  8 GB guidance governs: "Use Phi-3.5-mini-instruct (2.5 GB) or
  Qwen2.5-3B-Instruct 4-bit (~2 GB). Quality reduction is acceptable."
  Whether this needs a §7.1 amendment is a Batch 11 question, not settled here.

- config/settings.yaml names Qwen/Qwen2.5-7B-Instruct — §7.1's 12 GB
  recommendation. In 4-bit it lands around 5.5-6 GB, which does not leave
  working room on this card. Must be changed before Phase 4 runs.
  model_name is a config field (§5.15); no SDS amendment is required for it.

- AD-06 (embedding CPU-only, LLM GPU-only, never simultaneously) is
  load-bearing at this budget, not merely good practice.

- Laptop thermal note: the live pool holds 179 ENRICHED items (the fresh
  Batch 10 database). The Batch 11 direction caps one generation pass at
  pipeline.max_items_per_run (50), final after Stage 2. Generation runs at
  an 80 W cap. Sustained throttling is plausible and §7.2's
  throughput estimate was not written for a power-limited mobile GPU.