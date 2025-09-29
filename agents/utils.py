import config


def extract_token_usage(response) -> dict[str, int]:
    # extract token usage from LangChain response

    metadata = response.response_metadata
    usage = metadata["usage"]

    token_usage: dict = {
        "prompt_tokens": usage["input_tokens"],
        "completion_tokens": usage["output_tokens"],
        "total_tokens": usage.get(
            "total_tokens",
            usage["input_tokens"] + usage["output_tokens"],
        ),
    }

    return token_usage


def calculate_token_usage_cost(input_tokens: int, output_tokens: int, model_name: str) -> float:
    # calculate cost based on token usage and model pricing

    model_pricing = config.MODEL_PRICING[model_name]
    input_cost = (input_tokens / 1000) * model_pricing["input_cost_per_1k"]
    output_cost = (output_tokens / 1000) * model_pricing["output_cost_per_1k"]
    return input_cost + output_cost
