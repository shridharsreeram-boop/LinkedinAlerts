#!/usr/bin/env python3
"""
Unit tests for the Job Alert Pipeline.
Run with: python -m pytest AI_project/scripts/test_pipeline.py -v
or:        python AI_project/scripts/test_pipeline.py
"""

import unittest
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from run_pipeline import (
    _normalize_for_match,
    merge_cross_source_duplicates,
    is_expired,
)


class TestNormalizeForMatch(unittest.TestCase):

    def test_lowercases(self):
        self.assertEqual(_normalize_for_match("Senior Engineer"), "senior engineer")

    def test_strips_punctuation(self):
        self.assertEqual(_normalize_for_match("C++ Developer"), "c developer")

    def test_collapses_whitespace(self):
        self.assertEqual(_normalize_for_match("  Data  Analyst  "), "data analyst")

    def test_empty_string(self):
        self.assertEqual(_normalize_for_match(""), "")

    def test_none(self):
        self.assertEqual(_normalize_for_match(None), "")


class TestMergeCrossSourceDuplicates(unittest.TestCase):

    def _make_job(self, title, company, source, job_id):
        return {
            "id": job_id,
            "title": title,
            "company": {"display_name": company},
            "location": {"display_name": "Stockholm"},
            "redirect_url": f"https://example.com/{job_id}",
            "_source": source,
        }

    def test_no_duplicates(self):
        jobs = [
            self._make_job("Software Engineer", "Acme", "JobTech (Sweden)", "1"),
            self._make_job("Data Analyst", "Globex", "Adzuna (GB)", "2"),
        ]
        result = merge_cross_source_duplicates(jobs)
        self.assertEqual(len(result), 2)

    def test_detects_duplicate(self):
        jobs = [
            self._make_job("Software Engineer", "Acme AB", "JobTech (Sweden)", "1"),
            self._make_job("Software Engineer", "Acme AB", "Adzuna (GB)", "2"),
        ]
        result = merge_cross_source_duplicates(jobs)
        self.assertEqual(len(result), 1)

    def test_merged_entry_has_sources(self):
        jobs = [
            self._make_job("Project Manager", "Ericsson", "JobTech (Sweden)", "1"),
            self._make_job("Project Manager", "Ericsson", "Adzuna (DE)", "2"),
        ]
        result = merge_cross_source_duplicates(jobs)
        self.assertEqual(len(result), 1)
        self.assertIn("_sources", result[0])
        self.assertEqual(len(result[0]["_sources"]), 2)

    def test_case_insensitive_match(self):
        jobs = [
            self._make_job("software engineer", "acme ab", "JobTech (Sweden)", "1"),
            self._make_job("Software Engineer", "Acme AB", "Adzuna (GB)", "2"),
        ]
        result = merge_cross_source_duplicates(jobs)
        self.assertEqual(len(result), 1)

    def test_different_companies_not_merged(self):
        jobs = [
            self._make_job("Software Engineer", "Acme AB", "JobTech (Sweden)", "1"),
            self._make_job("Software Engineer", "Globex AB", "Adzuna (GB)", "2"),
        ]
        result = merge_cross_source_duplicates(jobs)
        self.assertEqual(len(result), 2)

    def test_empty_list(self):
        self.assertEqual(merge_cross_source_duplicates([]), [])


class TestIsExpired(unittest.TestCase):

    def test_future_date_not_expired(self):
        sub = {"end_date": "2099-12-31"}
        self.assertFalse(is_expired(sub))

    def test_past_date_expired(self):
        sub = {"end_date": "2020-01-01"}
        self.assertTrue(is_expired(sub))

    def test_today_not_expired(self):
        today = datetime.date.today().isoformat()
        sub = {"end_date": today}
        self.assertFalse(is_expired(sub))

    def test_no_end_date_not_expired(self):
        sub = {}
        self.assertFalse(is_expired(sub))


if __name__ == "__main__":
    unittest.main(verbosity=2)
