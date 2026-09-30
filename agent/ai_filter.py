def approve(model: str, symbol: str, closes: list[float], headlines: list[str]) -> bool:
    """Claude får bara svara JA/NEJ på en redan föreslagen köpsignal. Vid fel: NEJ."""
    try:
        import anthropic

        msg = anthropic.Anthropic().messages.create(
            model=model,
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    f"Regelbaserad signal: KÖP {symbol}. Senaste stängningar: {closes[-10:]}. "
                    f"Rubriker: {headlines or 'inga'}. Finns ett tydligt skäl att INTE köpa nu? "
                    "Svara bara NEJ (köp ok) eller JA (avstå)."
                ),
            }],
        )
        return msg.content[0].text.strip().upper().startswith("NEJ")
    except Exception:
        return False
