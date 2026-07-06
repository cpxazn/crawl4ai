"""Fork-specific field filtering for API responses (not in upstream).

Import this module in api.py to enable the `fields` query parameter
that lets callers trim crawl result payloads.
"""

from typing import List, Optional


def _filter_result_fields(result_dict: dict, fields: Optional[List[str]] = None) -> dict:
    """Filter a CrawlResult dict to only include specified fields.

    If fields is None or empty, returns the full dict unchanged.
    Supports nested field selection via dot notation (e.g. 'markdown.raw_markdown').
    """
    if not fields:
        return result_dict

    filtered = {}
    for field in fields:
        if '.' in field:
            # Nested field: e.g. 'markdown.raw_markdown'
            parent, child = field.split('.', 1)
            if parent in result_dict and isinstance(result_dict[parent], dict):
                if parent not in filtered:
                    filtered[parent] = {}
                if child in result_dict[parent]:
                    filtered[parent][child] = result_dict[parent][child]
        elif field in result_dict:
            filtered[field] = result_dict[field]
    return filtered
