# Pinned Qwen tokenizer assets

`qwen38_27b_tokenizer.json` and `qwen38_27b_chat_template.jinja` are unmodified
public assets from the [Qwen/Qwen3.8-27B model repository](https://huggingface.co/Qwen/Qwen3.8-27B/tree/72a217afab8029b39e4af1c7273a829995a3dbaf), revision
`72a217afab8029b39e4af1c7273a829995a3dbaf`. The repository identifies its
license as [Apache-2.0](https://huggingface.co/Qwen/Qwen3.8-27B/blob/72a217afab8029b39e4af1c7273a829995a3dbaf/README.md).
The full license is included in [LICENSE-Qwen-Apache-2.0.txt](LICENSE-Qwen-Apache-2.0.txt).

| File | SHA-256 |
| --- | --- |
| `qwen38_27b_tokenizer.json` | `0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3` |
| `qwen38_27b_chat_template.jinja` | `49d20d090bd09c0030f3f12d5e05c5e2c57c6864825ef2f640ca73e7578dfe24` |

The estimator verifies both hashes before first use, then keeps the verified
tokenizer and compiled template in memory. It performs no runtime download.
The local count is an admission estimate; Groq's private prompt rendering and
billing remain separately observed.
