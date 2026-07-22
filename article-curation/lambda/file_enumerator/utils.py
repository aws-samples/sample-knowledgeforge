"""
Utility Functions Module

Shared helper functions used across the file_enumerator lambda.
"""

import os

SOURCE_PREFIX = os.environ.get('SOURCE_PREFIX', '')
REGION = os.environ.get('AWS_REGION', '')


def tenant_s3_prefix(tenant_id):
    """Build the S3 prefix for a tenant's articles.
    Path: {SOURCE_PREFIX}/{tenant_id}/
    e.g. tenant_partitioning/itsm/snow/kb_articles/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/
    """
    return f'{SOURCE_PREFIX}/{tenant_id}/'


# COMMENTED OUT FOR RELEASE 1: Language detection disabled
# All articles in Release 1 are English - Comprehend API not needed
# Will be re-enabled in future releases when multi-language support is required
#
# def detect_language(text, comprehend_client):
#     """Detect language using Amazon Comprehend. Returns ISO language code (e.g. 'en').
#     Falls back to 'en' if detection fails or text is too short.
#     """
#     if not text or len(text.strip()) < 20:
#         return 'en'
#     try:
#         # Comprehend accepts max 5000 bytes
#         sample = text[:5000]
#         resp = comprehend_client.detect_dominant_language(Text=sample)
#         languages = resp.get('Languages', [])
#         if languages:
#             return languages[0]['LanguageCode']
#     except Exception as e:
#         from logger import get_logger
#         log = get_logger(lambda_name='file_enumerator')
#         log.warn('Language detection failed, defaulting to en', error=str(e))
#     return 'en'
