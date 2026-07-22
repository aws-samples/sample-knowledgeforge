"""
Input validation helpers for Lambda event payloads.

Usage:
    from validation import validate_event, validate_article
    validate_event(event, required_keys=['action'])
    validate_article(article, required_keys=['article_id', 'source_file_path'])
"""


class ValidationError(Exception):
    """Raised when event or article payload fails validation."""
    pass


def validate_event(event, required_keys=None):
    """Validate the top-level Lambda event payload.

    Args:
        event: The Lambda event dict.
        required_keys: Optional list of keys that must be present and non-empty.

    Raises:
        ValidationError if event is not a dict or required keys are missing/empty.
    """
    if not isinstance(event, dict):
        raise ValidationError(f'Event must be a dict, got {type(event).__name__}')

    if required_keys:
        for key in required_keys:
            if key not in event:
                raise ValidationError(f'Missing required key in event: {key}')
            val = event[key]
            if val is None or (isinstance(val, str) and not val.strip()):
                raise ValidationError(f'Key "{key}" in event is empty or null')


def validate_article(article, required_keys=None):
    """Validate a single article record from DynamoDB or event payload.

    Args:
        article: Article dict.
        required_keys: Keys that must be present and non-empty.

    Raises:
        ValidationError if article is invalid.
    """
    if not isinstance(article, dict):
        raise ValidationError(f'Article must be a dict, got {type(article).__name__}')

    default_keys = ['article_id', 'tenant_id']
    keys_to_check = required_keys or default_keys

    for key in keys_to_check:
        if key not in article:
            raise ValidationError(f'Missing required key in article: {key}')
        val = article[key]
        if val is None or (isinstance(val, str) and not val.strip()):
            raise ValidationError(f'Key "{key}" in article is empty or null')


def validate_quality_response(result, active_dimensions):
    """Validate a quality check or post-scoring LLM response before DynamoDB write.

    Args:
        result: Parsed JSON dict from LLM.
        active_dimensions: List of expected dimension names.

    Returns:
        Sanitised result dict with validated types and ranges.

    Raises:
        ValidationError if result is fundamentally invalid.
    """
    if not isinstance(result, dict):
        raise ValidationError(f'Quality response must be a dict, got {type(result).__name__}')

    # Validate and coerce quality_score
    raw_score = result.get('quality_score', 0)
    try:
        score = int(raw_score)
    except (ValueError, TypeError):
        score = 0
    score = max(0, min(100, score))
    result['quality_score'] = score

    # Validate per-dimension scores
    raw_scores = result.get('quality_scores', {})
    if not isinstance(raw_scores, dict):
        raw_scores = {}
    validated_scores = {}
    for dim in active_dimensions:
        val = raw_scores.get(dim)
        try:
            val = int(val)
        except (ValueError, TypeError):
            val = 0
        validated_scores[dim] = max(0, min(10, val))
    result['quality_scores'] = validated_scores

    return result


def validate_enrichment_response(result, expected_paragraph_count):
    """Validate an enrichment LLM response before applying to docx.

    Args:
        result: Parsed JSON dict from LLM.
        expected_paragraph_count: Number of paragraphs sent to LLM.

    Returns:
        Sanitised result dict.

    Raises:
        ValidationError if result is fundamentally invalid.
    """
    if not isinstance(result, dict):
        raise ValidationError(f'Enrichment response must be a dict, got {type(result).__name__}')

    paragraphs = result.get('paragraphs', [])
    if not isinstance(paragraphs, list):
        raise ValidationError(f'Enrichment paragraphs must be a list, got {type(paragraphs).__name__}')

    # Validate each paragraph has a 'rewritten' key with string value
    for i, p in enumerate(paragraphs):
        if not isinstance(p, dict):
            raise ValidationError(f'Paragraph {i} must be a dict, got {type(p).__name__}')
        if 'rewritten' not in p:
            raise ValidationError(f'Paragraph {i} missing "rewritten" key')
        if not isinstance(p['rewritten'], str):
            p['rewritten'] = str(p['rewritten'])

    return result
