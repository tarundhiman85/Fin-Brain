"""Shared checks for downloaded Ollama models."""
def validate_local_model(model: str, metadata: dict):
    tag = model.rsplit(":", 1)[-1].lower()
    details = metadata.get("details") or {}
    if (not model.strip() or tag == "cloud" or tag.endswith("-cloud")
            or metadata.get("remote_host") or metadata.get("remote_model")
            or details.get("remote_host") or details.get("remote_model")):
        raise ValueError("Select a downloaded local Ollama model.")
