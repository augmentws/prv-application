from dataclasses import dataclass


@dataclass(frozen=True)
class TokenEstimate:
    input_tokens: int
    output_tokens: int
    method: str
    version: str
    approximate: bool


def estimate_analysis_tokens(
    texts: list[str],
    *,
    stable_prefix: str,
    model: str,
    expected_output_tokens_per_document: int = 1200,
    request_count: int | None = None,
    reduce_input_overhead_characters: int = 0,
) -> TokenEstimate:
    request_count = request_count if request_count is not None else len(texts)
    combined = [stable_prefix] * request_count + texts
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        input_tokens = sum(len(encoding.encode(value)) for value in combined)
        input_tokens += (reduce_input_overhead_characters + 2) // 3
        return TokenEstimate(
            input_tokens=input_tokens,
            output_tokens=request_count * expected_output_tokens_per_document,
            method="tiktoken",
            version=f"tiktoken:{encoding.name}:v1",
            approximate=False,
        )
    except (ImportError, ValueError):
        characters = sum(len(value) for value in combined) + reduce_input_overhead_characters
        return TokenEstimate(
            input_tokens=(characters + 2) // 3,
            output_tokens=request_count * expected_output_tokens_per_document,
            method="conservative_character_estimate",
            version="characters-per-token-3:v1",
            approximate=True,
        )
