"""Спецификации API валидны и согласованы с кодом."""
from pathlib import Path

import yaml
from openapi_spec_validator import validate

from load_estimate import Params, estimate
from passport.access import CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


def test_openapi_is_valid():
    validate(yaml.safe_load((ROOT / "specs/openapi.yaml").read_text(encoding="utf-8")))


def test_openapi_categories_match_access_matrix():
    spec = yaml.safe_load((ROOT / "specs/openapi.yaml").read_text(encoding="utf-8"))
    assert spec["components"]["schemas"]["Category"]["enum"] == list(CATEGORIES)


def test_partner_posts_require_idempotency_key():
    spec = yaml.safe_load((ROOT / "specs/openapi.yaml").read_text(encoding="utf-8"))
    for path, item in spec["paths"].items():
        if path.startswith("/v1/partner/"):
            refs = [p.get("$ref") for p in item["post"]["parameters"]]
            assert "#/components/parameters/IdempotencyKey" in refs, path


def test_load_estimate_matches_case_numbers():
    r = estimate(Params(period_s=60))
    assert r["Телеметрия: сообщений на ребёнка в сутки"] == 1440
    worst = estimate(Params(concurrent=10_000, period_s=30))
    assert worst["Телеметрия: сообщений в сутки (пик)"] == 28_800_000
    assert estimate(Params())["Паспорт: событий в секунду (пик)"] < 50   # одного инстанса хватает
