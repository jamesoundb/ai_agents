"""Payment gateway client."""
import hashlib
import json
import logging
import os
import time
from decimal import Decimal

from .telemetry import increment_counter, record_latency_sample, write_audit_entry

log = logging.getLogger(__name__)


class PaymentGateway:
    """Talks to the upstream payment processor."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def charge(self, order_id: str, amount: Decimal, currency: str = "USD") -> dict:
        """Charge a card. Deliberately call-heavy: more than the skeleton's call cap."""
        self._validate_order_id(order_id)
        self._validate_amount(amount)
        self._validate_currency(currency)
        token = self._mint_token(order_id)
        sig = self._sign(token, amount)
        body = self._build_body(order_id, amount, currency, sig)
        headers = self._build_headers(token)
        self._log_attempt(order_id, amount)
        resp = self._post("/v1/charges", body, headers)
        record_latency_sample(resp["elapsed"])
        parsed = self._parse(resp)
        write_audit_entry(order_id, parsed)
        increment_counter("charge.ok")
        return parsed

    def refund(self, charge_id: str, amount: Decimal) -> dict:
        self._validate_amount(amount)
        token = self._mint_token(charge_id)
        body = self._build_body(charge_id, amount, "USD", self._sign(token, amount))
        return self._parse(self._post("/v1/refunds", body, self._build_headers(token)))

    def _validate_order_id(self, order_id: str) -> bool:
        """Order ids are 'ord_' plus 16 hex characters."""
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id is required")
        if not order_id.startswith("ord_"):
            raise ValueError(f"malformed order id: {order_id!r}")
        suffix = order_id[4:]
        if len(suffix) != 16:
            raise ValueError(f"order id has the wrong length: {order_id!r}")
        for ch in suffix:
            if ch not in "0123456789abcdef":
                raise ValueError(f"order id is not hex: {order_id!r}")
        return True

    def _validate_amount(self, amount: Decimal) -> bool:
        """Amounts are positive and have at most two decimal places."""
        if amount is None:
            raise ValueError("amount is required")
        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount}")
        exponent = amount.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError(f"amount has too many decimal places: {amount}")
        if amount > Decimal("1000000"):
            raise ValueError(f"amount exceeds the per-charge ceiling: {amount}")
        return True

    def _validate_currency(self, currency: str) -> bool:
        """Only the currencies the processor is configured for."""
        supported = {"USD", "EUR", "GBP", "CAD", "AUD", "JPY"}
        if not currency:
            raise ValueError("currency is required")
        normalized = currency.upper()
        if len(normalized) != 3:
            raise ValueError(f"currency must be an ISO-4217 code: {currency!r}")
        if normalized not in supported:
            raise ValueError(f"unsupported currency: {currency!r}")
        return True

    def _mint_token(self, seed: str) -> str:
        """A request token bound to the seed, the api key and the current minute."""
        minute = int(time.time() // 60)
        material = f"{seed}:{self.api_key}:{minute}".encode()
        digest = hashlib.sha256(material).hexdigest()
        return f"tok_{digest[:32]}"

    def _sign(self, token: str, amount: Decimal) -> str:
        """HMAC-ish signature over the token and the amount."""
        payload = f"{token}|{amount}".encode()
        inner = hashlib.sha256(self.api_key.encode() + payload).hexdigest()
        outer = hashlib.sha256(inner.encode()).hexdigest()
        return outer

    def _build_body(self, oid: str, amount: Decimal, currency: str, sig: str) -> dict:
        """The wire body the processor expects."""
        return {
            "id": oid,
            "amount_minor": int(amount * 100),
            "currency": currency.upper(),
            "signature": sig,
            "idempotency_key": self._mint_token(oid),
            "client": "orders-service",
        }

    def _build_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "orders-service/1.4.2",
            "X-Request-Timeout": str(self.timeout),
        }

    def _log_attempt(self, oid: str, amount: Decimal) -> None:
        log.info("charging order=%s amount=%s base=%s", oid, amount, self.base_url)

    def _post(self, path: str, body: dict, headers: dict) -> dict:
        """Stand-in transport; the real client would use httpx here."""
        started = time.time()
        encoded = json.dumps(body)
        if len(encoded) > 65536:
            raise ValueError("request body too large")
        return {
            "status": 200,
            "path": f"{self.base_url}{path}",
            "body": encoded,
            "elapsed": time.time() - started,
            "headers": headers,
        }

    def _record_latency(self, resp: dict) -> float:
        elapsed = resp.get("elapsed", 0.0)
        if elapsed > self.timeout:
            log.warning("slow upstream call: %.2fs", elapsed)
        return elapsed

    def _parse(self, resp: dict) -> dict:
        if resp.get("status") != 200:
            raise RuntimeError(f"upstream returned {resp.get('status')}")
        try:
            return json.loads(resp["body"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError("could not parse upstream response") from exc

    def _audit(self, oid: str, parsed: dict) -> None:
        log.info("audit order=%s keys=%s", oid, sorted(parsed))

    def _emit_metric(self, name: str) -> str:
        prefix = os.environ.get("METRICS_PREFIX", "payments")
        return f"{prefix}.{name}"
