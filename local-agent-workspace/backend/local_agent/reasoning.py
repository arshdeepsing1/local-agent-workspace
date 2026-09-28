"""Display only the reasoning summaries explicitly supplied by the provider."""


def reasoning_summary(content):
    if not isinstance(content, list):
        return ""
    return "".join(item["text"] for block in content
                   if isinstance(block, dict) and block.get("type") == "reasoning"
                   and isinstance(block.get("summary"), list)
                   for item in block["summary"]
                   if isinstance(item, dict) and item.get("type") == "summary_text"
                   and isinstance(item.get("text"), str))
