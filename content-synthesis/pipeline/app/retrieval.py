"""
Vector DB retrieval — query S3 Vectors for similar KB articles.

Tenant vector mapping:
  tenanta -> vector bucket: tenanta, index: tenanta-index
  tenantb -> vector bucket: tenantb, index: tenantb-index
  etc.
"""
import json
import logging

from app.config import PipelineConfig

logger = logging.getLogger(__name__)


def _get_vector_config(tenant: str) -> dict:
    """Return vector bucket name and index name for a tenant."""
    return {
        "vector_bucket": tenant,
        "index": f"{tenant}-index",
    }


def get_embedding(text: str, bedrock_client, config: PipelineConfig) -> list:
    """Generate embedding via Bedrock Titan."""
    resp = bedrock_client.invoke_model(
        modelId=config.bedrock.embedding_model_id,
        body=json.dumps({"inputText": text[:8000]}),
    )
    body = json.loads(resp["body"].read())
    return body["embedding"]


def _query_index(s3vectors_client, vector_bucket: str, index_name: str,
                 query_emb: list, top_k: int) -> list:
    """Query S3 Vectors index for similar articles."""
    try:
        results = s3vectors_client.query_vectors(
            vectorBucketName=vector_bucket,
            indexName=index_name,
            queryVector={"float32": query_emb},
            topK=top_k,
            returnMetadata=True,
            returnDistance=True,
        )
        return results.get("vectors", [])
    except Exception as e:
        logger.warning("query_vectors failed for %s/%s: %s",
                       vector_bucket, index_name, e)
        return []


def _get_vector_text(s3vectors_client, vector_bucket: str, index_name: str,
                     vector_key: str) -> str:
    """Try to pull article text from vector metadata."""
    try:
        resp = s3vectors_client.get_vectors(
            vectorBucketName=vector_bucket,
            indexName=index_name,
            keys=[vector_key],
            returnData=True,
            returnMetadata=True,
        )
        vectors = resp.get("vectors", [])
        if vectors:
            meta = vectors[0].get("metadata", {})
            for field in ["text", "content", "body", "article_text"]:
                if meta.get(field):
                    return str(meta[field])
    except Exception as e:
        logger.warning("get_vectors failed for %s: %s", vector_key, e)
    return ""


def retrieve_similar_articles(query_text: str, tenant: str,
                              clients: dict, config: PipelineConfig) -> list:
    """
    Retrieve top-K similar KB articles from the tenant's vector index.

    For each match, tries 3 approaches to get the full text:
    1. Text stored in query result metadata
    2. Text fetched via get_vectors on the vector key
    3. Fallback metadata summary
    """
    top_k = config.vectors.top_k
    vec_config = _get_vector_config(tenant)
    query_emb = get_embedding(query_text, clients["bedrock"], config)
    vectors = _query_index(clients["s3vectors"], vec_config["vector_bucket"],
                           vec_config["index"], query_emb, top_k)
    vectors = sorted(vectors, key=lambda v: v.get("distance", 1))[:top_k]

    articles = []
    for v in vectors:
        meta = v.get("metadata", {})
        doc_name = meta.get("document_name", v.get("key", ""))
        article_id = meta.get("article_id", "")
        full_text = ""

        # Approach 1: text in query result metadata
        for field in ["text", "content", "body", "article_text"]:
            if meta.get(field):
                full_text = str(meta[field])
                break

        # Approach 2: fetch via get_vectors
        if not full_text:
            vector_key = v.get("key", article_id or doc_name)
            if vector_key:
                full_text = _get_vector_text(
                    clients["s3vectors"], vec_config["vector_bucket"],
                    vec_config["index"], vector_key)

        # Approach 3: fallback metadata summary
        if not full_text and article_id:
            full_text = (
                f"[Existing KB article — text not available]\n"
                f"Article ID: {article_id}\n"
                f"Source: {meta.get('source_system', 'ITSM_KB')}\n"
            )

        if full_text:
            articles.append({
                "document_name": doc_name or article_id or v.get("key", "unknown"),
                "text": full_text,
                "distance": v.get("distance"),
            })

    logger.info("Retrieved %d articles for tenant '%s'", len(articles), tenant)
    return articles
