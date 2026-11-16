"""Step C: independently review and minimally edit a generated idea."""
# Reconstructed approximately from the preserved Python 3.14 bytecode.

from __future__ import annotations
import json
import hashlib
from pathlib import Path
from typing import Any
from .distribution import TopicDraw
MODEL_ID = 'claude-sonnet-5-5'
PROMPTS_DIR = (Path(__file__).resolve().parents[1] / 'prompts')
IDEA_FIELDS = {'category', 'core_challenge', 'difficulty_rationale', 'environment_sketch', 'expected_deliverable', 'feasibility_rationale', 'objective', 'reference_task_ids', 'repository_selection', 'risks', 'scenario', 'subcategory', 'title', 'verification_approach'}
PRESERVED_FIELDS = ('category', 'subcategory', 'objective', 'expected_deliverable', 'core_challenge', 'reference_task_ids', 'repository_selection')

def _parse_json(text):
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError('Step C response must be a JSON object')
    return value

def render_editor_prompt(topic, idea, prior_feedback=None):
    template = (PROMPTS_DIR / 'idea_editor.md').read_text(encoding='utf-8')
    return template.replace('{{CATEGORY}}', topic.category).replace('{{SUBCATEGORY}}', topic.subcategory).replace('{{IDEA_JSON}}', json.dumps(idea, indent=2, ensure_ascii=False)).replace('{{PRIOR_EDITOR_FEEDBACK}}', (prior_feedback or 'None; this is the first idea for this draw.'))

def review_idea(client, topic, idea, prior_feedback=None):
    response = client.messages.create(model=MODEL_ID, max_tokens=8192, system='You are the independent Step C concept editor. Follow the narrow edit/restart rubric and return JSON only.', messages=[{'role': 'user', 'content': render_editor_prompt(topic, idea, prior_feedback)}])
    text = """
""".join((getattr(block, 'text', '') for block in response.content if getattr(block, 'type', '') == 'text'))
    result = _parse_json(text)
    usage = getattr(response, 'usage', None)
    result['model_id'] = MODEL_ID
    result['usage'] = {'input_tokens': getattr(usage, 'input_tokens', 0), 'output_tokens': getattr(usage, 'output_tokens', 0)}
    result['prompt_sha256'] = hashlib.sha256(render_editor_prompt(topic, idea, prior_feedback).encode('utf-8')).hexdigest()
    if result.get('decision') not in frozenset({'continue', 'edit', 'restart'}):
        raise ValueError('Step C decision must be continue, edit, or restart')
    if (not isinstance(result.get('reasoning'), list) or not isinstance(result.get('edits'), list)):
        raise ValueError('Step C must return reasoning and edits arrays')
    if not isinstance(result.get('unresolved_risks'), list):
        raise ValueError('Step C must return unresolved_risks array')
    if result['decision'] == 'restart':
        if (result.get('edited_idea') is not None or result['edits']):
            raise ValueError('Step C restart must not contain an edited idea or edits')
        if (not isinstance(result.get('restart_feedback'), str) or not result['restart_feedback'].strip()):
            raise ValueError('Step C restart requires brief abstract feedback')
        return result
    edited = result.get('edited_idea')
    if (not isinstance(edited, dict) or set(edited) != IDEA_FIELDS):
        raise ValueError('Step C must return a complete idea object with the required fields')
    changed_fields = [field for field in PRESERVED_FIELDS if edited.get(field) != idea.get(field)]
    if changed_fields:
        field_list = ', '.join(changed_fields)
        feedback = (('Step C changed protected field(s): ' + field_list) + '. Start from a fresh idea in the same sampled category and subcategory, and make the concept clear enough that Step C can review it without changing its category, subcategory, objective, expected deliverable, core challenge, reference task IDs, or repository selection.')
        result['decision'] = 'restart'
        result['reasoning'] = [*result['reasoning'], (('Step C changed protected field(s): ' + field_list) + '; the workflow will restart idea generation.')]
        result['edits'] = []
        result['edited_idea'] = None
        result['restart_feedback'] = feedback
        return result
    if result.get('restart_feedback') is not None:
        raise ValueError('Step C continue/edit must set restart_feedback to null')
    return result
