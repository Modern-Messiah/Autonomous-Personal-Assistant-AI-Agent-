"""Mortgage-related helpers used during enrichment."""

from __future__ import annotations

from typing import Protocol


class InterestRateProviderProtocol(Protocol):
    """Contract for providers that return current annual mortgage rate."""

    async def get_annual_rate(self) -> float: ...


class StaticInterestRateProvider:
    """Simple provider returning a fixed annual interest rate."""

    def __init__(self, annual_rate_percent: float = 17.5) -> None:
        self._annual_rate_percent = annual_rate_percent

    async def get_annual_rate(self) -> float:
        return self._annual_rate_percent


def calculate_annuity_payment(
    *,
    principal_kzt: int,
    annual_rate_percent: float,
    years: int,
) -> tuple[int, int]:
    """Return (monthly_payment, total_overpayment) using annuity formula."""
    if principal_kzt <= 0 or years <= 0:
        return 0, 0

    months = years * 12
    monthly_rate = annual_rate_percent / 100 / 12

    if monthly_rate <= 0:
        monthly_payment = round(principal_kzt / months)
    else:
        growth = (1 + monthly_rate) ** months
        monthly_payment = round(principal_kzt * monthly_rate * growth / (growth - 1))

    total_paid = monthly_payment * months
    overpayment = max(total_paid - principal_kzt, 0)
    return monthly_payment, overpayment

