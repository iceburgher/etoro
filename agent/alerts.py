"""Larm. Leverantören är utbytbar; ett misslyckat larm stoppar aldrig en stängning eller ett jobb."""
import json

import requests

NORMAL, CRITICAL = "normal", "critical"


class Alerter:
    def send(self, level: str, kind: str, message: str, data: dict) -> None:
        raise NotImplementedError


class LogAlerter(Alerter):
    """Skriver larmet till loggen. Används i tester, lokalt och som reserv."""
    def __init__(self, log):
        self.log = log

    def send(self, level, kind, message, data):
        self.log(event="alert", level=level, kind=kind, message=message, data=data)


class ResendEmailAlerter(Alerter):
    """E-post via Resend (HTTP-API, fungerar i Vercel). Byts lätt mot annan leverantör."""
    def __init__(self, api_key: str, sender: str, to: list[str], timeout: float = 5.0):
        self.api_key, self.sender, self.to, self.timeout = api_key, sender, to, timeout

    def send(self, level, kind, message, data):
        prefix = "[KRITISKT] " if level == CRITICAL else ""
        r = requests.post("https://api.resend.com/emails", timeout=self.timeout,
                          headers={"Authorization": f"Bearer {self.api_key}"},
                          json={"from": self.sender, "to": self.to,
                                "subject": f"{prefix}Guldagenten: {kind}",
                                "text": f"{message}\n\n{json.dumps(data, indent=2, ensure_ascii=False, default=str)}"})
        r.raise_for_status()


class SafeAlerter:
    """Omsluter en leverantör: loggar alltid, och sväljer leverantörsfel (loggas som alert_failed)."""
    def __init__(self, provider: Alerter | None, log):
        self.provider, self.log = provider, log
        self.sent: list[tuple[str, str]] = []

    def __call__(self, level: str, kind: str, message: str, **data) -> None:
        self.sent.append((level, kind))
        self.log(event="alert", level=level, kind=kind, message=message, data=data)
        if not self.provider:
            return
        try:
            self.provider.send(level, kind, message, data)
        except Exception as e:  # larm får aldrig blockera handel
            self.log(event="alert_failed", kind=kind, error=str(e))
