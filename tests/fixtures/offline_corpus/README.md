# Pensae synthetic offline evaluation corpus

This directory contains the versioned Phase 5 evaluation corpus. Every source and expected output
is original synthetic text written for Pensae testing. It is not copied from a live page, model
response, customer record, or proprietary dataset.

- `manifest.json` is the authoritative machine-readable inventory and expected-outcome contract.
- `LICENSE.md` grants CC0-1.0 reuse of the corpus content.
- URLs use reserved `.example` domains and are identifiers only; tests never request them.
- Tests may construct transient spans from source text, but full source text is never product data.

The corpus is deliberately small. It covers evidence integrity, independent signals, negative
evidence, source concentration, exact/semantic identity labels, strict schema repair, fixed roles
and workflow, and retrieved-text prompt injection. It is not a substitute for the signed G7 live
model and retrieval checklist.
