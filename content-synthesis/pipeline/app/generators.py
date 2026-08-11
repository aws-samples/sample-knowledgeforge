"""
Article generators — Bedrock streaming, KB and RCA generation.

All configuration (model IDs, guardrail settings, prompt templates,
ticket limits) is read from a PipelineConfig object passed into each
public function.
"""
import asyncio
import json
import logging
import random

# Use a CSPRNG-backed generator (SystemRandom) for jitter/sampling so static
# analysis does not flag the default non-cryptographic PRNG (CWE-338).
_secure_random = random.SystemRandom()
from datetime import datetime

from app.config import PipelineConfig

logger = logging.getLogger(__name__)

SHORT_DESC_DELIMITER = "===SHORT_DESCRIPTION==="

# Claude Sonnet has ~200k token context. Reserve ~50k for system prompt + output.
# 1 token ≈ 4 chars → 150k tokens ≈ 600k chars, but in practice Bedrock's
# tokenizer is stricter. Use a conservative 400k char limit (~100k tokens).
MAX_PROMPT_CHARS = 400_000


# ── Bedrock streaming helpers ────────────────────────────────

def _collect_stream(resp) -> tuple[str, dict]:
    """Consume a Bedrock streaming response, return (full_text, usage_dict)."""
    chunks = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for event in resp["body"]:
        chunk = json.loads(event["chunk"]["bytes"])
        if chunk.get("type") == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                chunks.append(delta.get("text", ""))
        elif chunk.get("type") == "message_delta":
            u = chunk.get("usage", {})
            if u:
                usage["output_tokens"] = u.get("output_tokens", 0)
        elif chunk.get("type") == "message_start":
            msg = chunk.get("message", {})
            u = msg.get("usage", {})
            if u:
                usage["input_tokens"] = u.get("input_tokens", 0)
    return "".join(chunks), usage


async def _invoke_stream(clients, config: PipelineConfig,
                         system_prompt: str, user_prompt: str,
                         temperature: float = 0.1) -> tuple[str, dict]:
    """Async wrapper — runs the blocking Bedrock streaming call in an executor."""
    loop = asyncio.get_running_loop()

    def _call():
        kwargs = {
            "modelId": config.bedrock.llm_model_id,
            "body": json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": config.bedrock.max_tokens,
                "temperature": temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }),
        }
        if config.guardrail.guardrail_id:
            kwargs["guardrailIdentifier"] = config.guardrail.guardrail_id
            kwargs["guardrailVersion"] = config.guardrail.guardrail_version
        resp = clients["bedrock"].invoke_model_with_response_stream(**kwargs)
        return _collect_stream(resp)

    return await loop.run_in_executor(None, _call)


# ── Context assembly ─────────────────────────────────────────

def assemble_theme_context(theme_name: str, theme_data: dict,
                           config: PipelineConfig) -> dict:
    """Build the full context payload from a theme dict entry.
    Randomly samples up to max_sample_tickets tickets, or uses all if fewer.
    Caps individual work notes at max_notes_per_ticket chars.
    """
    max_sample_tickets = config.pipeline.max_sample_tickets
    max_notes_per_ticket = config.pipeline.max_notes_per_ticket

    all_tickets = []
    for ticket_id, ticket in theme_data.get("tickets", {}).items():
        notes = ticket.get("comments_and_work_notes", "")
        all_tickets.append({
            "ticket_id": ticket_id,
            "short_description": ticket.get("short_description", ""),
            "comments_and_work_notes": notes[:max_notes_per_ticket],
        })

    # Sample tickets: use all if <= max_sample_tickets, else random sample
    if len(all_tickets) > max_sample_tickets:
        sampled = _secure_random.sample(all_tickets, max_sample_tickets)
        logger.info("Sampled %d of %d tickets for theme '%s'",
                     max_sample_tickets, len(all_tickets), theme_name)
    else:
        sampled = all_tickets

    return {
        "theme_name": theme_name,
        "keywords": theme_data.get("keywords", []),
        "article_scope": theme_data.get("article_scope", ""),
        "ticket_count": theme_data.get("ticket_count", 0),
        "tickets": sampled,
    }


def _format_context_for_prompt(context: dict) -> str:
    """Render context into a prompt string, truncating oldest tickets if too large."""
    header = (
        f"Theme: {context['theme_name']}\n"
        f"Keywords: {', '.join(context['keywords'])}\n"
        f"Scope: {context['article_scope']}\n"
        f"Total Tickets: {context['ticket_count']}\n\n"
        f"Ticket Data:\n\n"
    )
    ticket_blocks = []
    for t in context["tickets"]:
        ticket_blocks.append(
            f"--- Ticket: {t['ticket_id']} ---\n"
            f"Short Description: {t['short_description']}\n"
            f"Work Notes:\n{t['comments_and_work_notes']}\n"
        )

    full_text = header + "\n".join(ticket_blocks)
    if len(full_text) <= MAX_PROMPT_CHARS:
        return full_text

    truncated = 0
    while ticket_blocks and len(header + "\n".join(ticket_blocks)) > MAX_PROMPT_CHARS:
        ticket_blocks.pop(0)
        truncated += 1
    if truncated:
        logger.warning(
            "Truncated %d oldest tickets for theme '%s' to fit token limit",
            truncated, context["theme_name"],
        )
    return header + "\n".join(ticket_blocks)


def _parse_response(full_text: str, fallback_prefix: str,
                    theme_name: str) -> tuple[str, str]:
    """Split LLM response into article body and short_description."""
    if SHORT_DESC_DELIMITER in full_text:
        parts = full_text.split(SHORT_DESC_DELIMITER, 1)
        return parts[0].strip(), parts[1].strip()[:200]
    logger.warning(
        "Short description delimiter not found for theme '%s', using fallback",
        theme_name,
    )
    return full_text.strip(), f"{fallback_prefix}: {theme_name}"[:200]


def _format_retrieved_articles(retrieved: list) -> tuple[str, str]:
    """Format retrieved KB articles into a context block and grounding instruction."""
    populated = [a for a in retrieved if a.get("text", "").strip()]
    has_full_text = any(
        not a["text"].startswith("[Existing KB article") for a in populated
    )

    if not populated:
        return "", (
            "WARNING: No reference KB articles available. "
            "Generate the article based on the ticket data only. "
            "Mark any steps as '[General guidance — verify with your IT team].'"
        )

    context = "\n\n---\n\n".join(
        f"Source: {a['document_name']}\n{a['text'][:3000]}"
        for a in populated
    )

    if has_full_text:
        grounding = (
            "IMPORTANT: Use the reference KB articles below as grounding context "
            "for structure, terminology, and known procedures. "
            "Combine this with the ticket data to produce a comprehensive article. "
            "Do NOT invent procedures, commands, URLs, or system names "
            "not present in either the reference articles or the ticket data."
        )
    else:
        grounding = (
            "NOTE: Similar KB articles exist but their full text is not available. "
            "Use the ticket data as the primary source. "
            "Mark specific procedures as '[Verify with your IT team].'"
        )

    return f"Reference KB Articles:\n\n{context}", grounding


# ── KB article generation ────────────────────────────────────

async def generate_kb_article(
    context: dict, clients: dict, config: PipelineConfig,
    counter=None, retrieved_articles: list = None,
) -> tuple[str, str, dict]:
    """Generate a KB article. Returns (article_text, short_description, usage)."""
    month = datetime.now().strftime("%B")
    year = datetime.now().strftime("%Y")

    ref_block, grounding = _format_retrieved_articles(retrieved_articles or [])

    system_prompt = config.prompts.kb_system.format(
        month=month,
        year=year,
        grounding=grounding,
        SHORT_DESC_DELIMITER=SHORT_DESC_DELIMITER,
    )

    context_text = _format_context_for_prompt(context)
    user_prompt = (
        f"Generate a comprehensive IT Knowledge Base article based on "
        f"the following themed ticket data and reference articles.\n\n"
        f"{ref_block}\n\n" if ref_block else
        f"Generate a comprehensive IT Knowledge Base article based on "
        f"the following themed ticket data.\n\n"
    )
    user_prompt += (
        f"{context_text}\n\n"
        f"Write a detailed, well-structured KB article grounded in "
        f"the data above."
    )

    full_text, usage = await _invoke_stream(clients, config, system_prompt, user_prompt)
    article_text, short_desc = _parse_response(full_text, "KB Article", context["theme_name"])

    if counter:
        await counter.record(f"{context['theme_name']}:generate_kb_article", usage)
    return article_text, short_desc, usage


# ── RCA article generation ───────────────────────────────────

async def generate_rca_article(
    context: dict, clients: dict, config: PipelineConfig,
    counter=None, retrieved_articles: list = None,
) -> tuple[str, str, dict]:
    """Generate an RCA article. Returns (article_text, short_description, usage)."""
    ref_block, grounding = _format_retrieved_articles(retrieved_articles or [])

    system_prompt = config.prompts.rca_system.format(
        grounding=grounding,
        SHORT_DESC_DELIMITER=SHORT_DESC_DELIMITER,
    )

    context_text = _format_context_for_prompt(context)
    user_prompt = (
        f"Generate a comprehensive Root Cause Analysis (RCA) document based on "
        f"the following themed ticket data and reference articles.\n\n"
        f"{ref_block}\n\n" if ref_block else
        f"Generate a comprehensive Root Cause Analysis (RCA) document based on "
        f"the following themed ticket data.\n\n"
    )
    user_prompt += (
        f"{context_text}\n\n"
        f"Write a detailed, well-structured RCA document grounded in "
        f"the data above."
    )

    full_text, usage = await _invoke_stream(clients, config, system_prompt, user_prompt)
    article_text, short_desc = _parse_response(full_text, "RCA", context["theme_name"])

    if counter:
        await counter.record(f"{context['theme_name']}:generate_rca_article", usage)
    return article_text, short_desc, usage
