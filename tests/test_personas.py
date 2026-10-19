import asyncio
import json

from swarm_persona import planner
from swarm_persona.config import Config


def cfg(personas=None, journeys=None) -> Config:
    return Config.model_validate({
        'app': {'name': 'A', 'description': 'd', 'url': 'https://preview.example.com'},
        'journeys': journeys or [{'name': 'write', 'goal': 'Write a note.', 'start_path': '/notes',
                                  'success_criteria': 'the note is listed', 'devices': ['desktop']}],
        'personas': {'count': 3, **(personas or {})}})


def fake_llm(monkeypatch, reply):
    prompts = []

    async def complete(llm, prompt, max_tokens=12000):
        prompts.append(prompt)
        return json.dumps(reply), 42

    monkeypatch.setattr(planner, 'complete', complete)
    return prompts


SAVED = [{'name': 'Priya', 'background': 'Runs a studio.', 'tech_comfort': 'high', 'patience': 'low'},
         {'name': 'Tom', 'background': 'New account manager.', 'tech_comfort': 'low'}]


def test_saved_personas_take_the_seats_in_order_and_only_the_rest_are_generated(monkeypatch):
    prompts = fake_llm(monkeypatch, [{'name': 'Gen', 'who': 'a nurse', 'tech_comfort': 'low', 'patience': 'high'}])
    personas, tokens = asyncio.run(planner.plan(cfg({'saved': SAVED}), {'title': '', 'body': '', 'files': []},
                                                cfg().journeys))
    assert [p['name'] for p in personas] == ['Priya', 'Tom', 'Gen']
    assert (personas[0]['who'], personas[0]['tech_comfort'], personas[0]['patience']) == ('Runs a studio.', 'high', 'low')
    assert personas[1]['patience'] == 'medium' and personas[2]['saved'] is False and personas[0]['saved'] is True
    assert all(p['goal'] == 'Write a note.' for p in personas)
    assert tokens == 42 and len(prompts) == 1
    assert '1. device' in prompts[0] and '2. device' not in prompts[0]  # one seat left to write


def test_no_model_call_when_saved_personas_fill_every_seat(monkeypatch):
    prompts = fake_llm(monkeypatch, [])
    many = SAVED + [{'name': 'Ana', 'background': 'A teacher.'}, {'name': 'Extra', 'background': 'Unused.'}]
    personas, tokens = asyncio.run(planner.plan(cfg({'saved': many}), {'title': '', 'body': '', 'files': []},
                                                cfg().journeys))
    assert [p['name'] for p in personas] == ['Priya', 'Tom', 'Ana'] and tokens == 0 and prompts == []


def test_the_audience_reaches_the_persona_prompt(monkeypatch):
    prompts = fake_llm(monkeypatch, [])
    c = cfg({'audience': 'Retired teachers on tablets'})
    personas, _ = asyncio.run(planner.plan(c, {'title': '', 'body': '', 'files': []}, c.journeys))
    assert '<audience>Retired teachers on tablets</audience>' in prompts[0]
    assert [p['name'] for p in personas] == ['Tester 1', 'Tester 2', 'Tester 3']
    assert '<audience>' not in fake_prompt_without_audience(monkeypatch)


def fake_prompt_without_audience(monkeypatch):
    prompts = fake_llm(monkeypatch, [])
    asyncio.run(planner.plan(cfg(), {'title': '', 'body': '', 'files': []}, cfg().journeys))
    return prompts[0]


def test_saved_personas_are_validated():
    import pytest

    for bad in [{'name': '', 'background': 'x'}, {'name': 'A', 'background': 'x', 'patience': 'zero'},
                {'name': 'A', 'background': 'x', 'age': 3}]:
        with pytest.raises(ValueError):
            cfg({'saved': [bad]})


def test_tech_comfort_reaches_the_agent_task():
    from swarm_persona.agent import task_prompt

    persona = {'name': 'Tom', 'who': 'x', 'device': 'mobile', 'tech_comfort': 'low', 'patience': 'high',
               'goal': 'g', 'success_criteria': '', 'start_path': '/'}
    text = task_prompt(cfg(), {'title': ''}, persona, 'https://preview.example.com')
    assert 'comfort with technology is\nlow' in text
