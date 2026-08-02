from src.fetch_avanza import build_summary, normalize


def test_normalize_and_summary():
    rows = [
        {
            "orderbookId": "1",
            "name": "BULL TEST X10 SG",
            "issuer": "Societe Generale",
            "direction": "Long",
            "totalValueTraded": 1000,
            "underlyingInstrument": {"name": "Test AB"},
            "leverage": 10,
        },
        {
            "orderbookId": "2",
            "name": "BEAR TEST X10 VT",
            "issuer": "Vontobel",
            "direction": "Short",
            "totalValueTraded": 500,
            "underlyingInstrument": {"name": "Test AB"},
            "leverage": 10,
        },
    ]
    normalized = normalize(rows, "certificate")
    summary = build_summary(normalized)

    assert summary["number_of_products"] == 2
    assert summary["total_turnover_sek"] == 1500
    assert summary["top_underlyings_by_turnover"][0]["name"] == "Test AB"
