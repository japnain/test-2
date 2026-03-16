"""Tests for market scanner parsing."""

from polyarb.client import PolymarketClient
from polyarb.config import Config


class TestMarketParsing:
    def test_parse_neg_risk_markets(self):
        """Test that NegRisk markets are grouped by event."""
        config = Config()
        client = PolymarketClient(config)

        raw_markets = [
            {
                "id": 1,
                "question": "Will Alice win?",
                "conditionId": "cond_1",
                "slug": "alice-wins",
                "clobTokenIds": '["token_alice_yes", "token_alice_no"]',
                "outcomes": '["Yes", "No"]',
                "negRisk": True,
                "negRiskMarketID": "event_election",
                "groupItemTitle": "2024 Election",
                "minimumTickSize": "0.01",
                "minimumOrderSize": "5.0",
            },
            {
                "id": 2,
                "question": "Will Bob win?",
                "conditionId": "cond_2",
                "slug": "bob-wins",
                "clobTokenIds": '["token_bob_yes", "token_bob_no"]',
                "outcomes": '["Yes", "No"]',
                "negRisk": True,
                "negRiskMarketID": "event_election",
                "groupItemTitle": "2024 Election",
                "minimumTickSize": "0.01",
                "minimumOrderSize": "5.0",
            },
        ]

        events = client.parse_markets_into_events(raw_markets)

        # Should be grouped into 1 event with 2 outcomes
        neg_risk_events = [e for e in events if e.neg_risk]
        assert len(neg_risk_events) == 1
        assert neg_risk_events[0].num_outcomes == 2
        assert neg_risk_events[0].outcomes[0].token_id == "token_alice_yes"
        assert neg_risk_events[0].outcomes[1].token_id == "token_bob_yes"

    def test_parse_binary_markets(self):
        """Test that binary markets create standalone events."""
        config = Config()
        client = PolymarketClient(config)

        raw_markets = [
            {
                "id": 1,
                "question": "Will it rain?",
                "conditionId": "cond_rain",
                "slug": "will-it-rain",
                "clobTokenIds": '["token_yes", "token_no"]',
                "outcomes": '["Yes", "No"]',
                "negRisk": False,
                "minimumTickSize": "0.01",
                "minimumOrderSize": "1.0",
            },
        ]

        events = client.parse_markets_into_events(raw_markets)
        binary = [e for e in events if not e.neg_risk]
        assert len(binary) == 1
        assert binary[0].num_outcomes == 2

    def test_parse_missing_clob_tokens(self):
        """Markets without CLOB tokens should be skipped."""
        config = Config()
        client = PolymarketClient(config)

        raw_markets = [
            {
                "id": 1,
                "question": "No tokens",
                "conditionId": "cond_1",
                "clobTokenIds": None,
                "outcomes": '["Yes", "No"]',
                "negRisk": False,
            },
        ]

        events = client.parse_markets_into_events(raw_markets)
        assert len(events) == 0
