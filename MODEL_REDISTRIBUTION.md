# Model acquisition and non-redistribution policy

Pensae Signal contains no model weights. Bootstrap, tests, packaging, and release checks do not download,
vendor, copy, or redistribute GGUF, SafeTensors, checkpoints, or any derived model artifact.

The protected runtime expects these operator-acquired artifacts:

| Runtime role | Protected identifier | Distribution status |
|---|---|---|
| Chat | `Qwen3.6-35B-A3B-UD-IQ4_XS` | `unsloth/Qwen3.6-35B-A3B-GGUF`, Apache-2.0; acquire separately; not redistributed by Pensae Signal. |
| Embedding | `Qwen3-Embedding-0.6B` (GGUF Q8_0 runtime artifact) | Official GGUF: `Qwen/Qwen3-Embedding-0.6B-GGUF`, Apache-2.0. Acquire separately; not redistributed by Pensae Signal. |

Before acquisition, the operator must identify the exact upstream base model, conversion, and
quantization source; read and accept every applicable license and use restriction; verify the
artifact checksum; and record approval without copying license-gated payloads into the repository.
The protected identifier alone does not establish a model artifact's provenance or license.

Reviewed upstream records:

- Chat base model and license: <https://huggingface.co/Qwen/Qwen3.6-35B-A3B>
- Protected UD-IQ4_XS conversion record (17.7 GB, published SHA-256
  `649d7508507b84638732c4f52c24c8b15843c6dca2f3ff793ae07c14a67ebbb3`):
  <https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/blob/main/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf>
- Official embedding Q8_0 GGUF (639 MB, published SHA-256
  `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`) and license:
  <https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/blob/main/Qwen3-Embedding-0.6B-Q8_0.gguf>

The live G7 record must name and hash the actual local files and compare them with the published
artifact identities. Upstream metadata is evidence of provenance and terms, but the operator must
still approve the model licenses and the actual acquired files before G7.

Do not redistribute model weights with Pensae Signal, and re-verify upstream model, conversion, and
quantization terms before any redistribution, mirroring, repackaging, or transfer to another
operator. A changed model identifier, quantization, source, or license is a release review trigger.

Local `.env` paths and model files remain excluded by `.gitignore`. The release audit rejects common
model-weight extensions if a file enters the release payload. This policy is a release control and
is not legal advice.
