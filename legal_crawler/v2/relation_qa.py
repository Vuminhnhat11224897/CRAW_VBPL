# Vendored from data_vbpl (legal_ingest) commit 22d9796; logic unchanged. See docs/adr/0001-v3-cluster-v2-schema.md.
"""Schemas and validators for short, complete relation QA responses."""

VERDICT_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['valid', 'reason'],
    'properties': {
        'valid': {'type': 'boolean'},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 240},
    },
}

SCOPE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['scope', 'correct', 'evidence'],
    'properties': {
        'scope': {'type': 'string', 'minLength': 1, 'maxLength': 200},
        'correct': {'type': 'boolean'},
        'evidence': {'type': 'string', 'maxLength': 1200},
    },
}


def validate_verdict(payload, *, bounded=True):
    if not isinstance(payload, dict) or set(payload) != {'valid', 'reason'}:
        raise ValueError('QA must return only valid and reason')
    if type(payload['valid']) is not bool:
        raise ValueError('QA must return boolean valid')
    reason = payload['reason']
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('QA reason must be nonempty text')
    if bounded and (len(reason) > 240 or len(reason.split()) > 30):
        raise ValueError('QA reason must be nonempty, at most 30 words and 240 characters')


def validate_scope(payload):
    if not isinstance(payload, dict) or set(payload) != {'scope', 'correct', 'evidence'}:
        raise ValueError('Scope QA must return only scope, correct and evidence')
    if type(payload['correct']) is not bool:
        raise ValueError('Scope QA must return boolean correct')
    if not isinstance(payload['scope'], str) or not payload['scope'].strip() or len(payload['scope']) > 200:
        raise ValueError('Scope QA must return a nonempty scope of at most 200 characters')
    if not isinstance(payload['evidence'], str) or len(payload['evidence']) > 1200:
        raise ValueError('Scope QA evidence must be a string of at most 1200 characters')
    if payload['correct'] and not payload['evidence'].strip():
        raise ValueError('A correct scope needs source evidence')


class QAIncompleteError(ValueError):
    """Verified relations were saved, but some groups still need QA."""
