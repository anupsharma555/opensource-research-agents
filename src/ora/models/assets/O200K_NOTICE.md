# o200k_base vocabulary notice

`o200k_base.tiktoken` is the OpenAI vocabulary used by the pinned
`tiktoken==0.11.0` `o200k_harmony` encoding. The
[`openai/tiktoken` version 0.11.0 loader](https://github.com/openai/tiktoken/blob/0.11.0/tiktoken_ext/openai_public.py)
pins the checksum and fetches the vocabulary from the
[public asset URL](https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken).
The retained file has 3,613,922 bytes and SHA-256
`446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`.

The [repository license](https://github.com/openai/tiktoken/blob/0.11.0/LICENSE)
is MIT. An [OpenAI repository collaborator confirmed](https://github.com/openai/tiktoken/issues/92)
that it also applies to the encoding files. The required copyright and
permission notice is preserved in `LICENSE-tiktoken.txt` next to this asset.
