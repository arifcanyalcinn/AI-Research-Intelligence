# Current Project State

Current Phase:
Phase 1

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

Implemented Sources

[x] ArXiv
[x] HuggingFace Papers
[x] HuggingFace Models
[x] HuggingFace Spaces
[x] GitHub Trending
[DEFERRED] PapersWithCode

Current Test Count

739 Passed
661 Passed, 2 Skipped without the [embedding] extra

Architecture Status

- Frozen SDS compliant
- No known architectural violations

Known Compatibility Issues

- respx 0.21.1 issue #277
- Temporary workaround: url__startswith
- Remove after upgrading to respx >= 0.22


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

- Laptop thermal note: the first Phase 4 live run processes 306 ENRICHED
  items at an 80 W cap. Sustained throttling is plausible and §7.2's
  throughput estimate was not written for a power-limited mobile GPU.