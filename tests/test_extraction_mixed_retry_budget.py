"""Installed memgarden must enforce one shared retry budget through host extraction.

No source-path overrides: CI exercises the actual dependency installed from the
hash-locked requirements. Keep these controls red until a released fixed artifact
is selected; a source-only component PASS is not host integration acceptance.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from memgarden import GardenComponent, CaptureRequest, MaintenanceRequest
from model_api_runtime.v2 import extraction

OBSERVATIONS = []

class HostMixedBudget(unittest.TestCase):
    def case(self, lane, first, empty):
        key = 'cards' if lane == 'capture' else 'consolidations'
        first_reply = '{"' + key + '": invalid}'
        if first == 'semantic':
            first_reply = json.dumps({'cards': [{'action': 'supersede', 'bucket': 'Facts',
                'summary': 'Updated fact', 'content': 'Synthetic fact without target.',
                'importance': 0.5, 'pulse': 0.2}]})
        second_reply = '' if empty else '{"' + key + '":['
        replies = [(first_reply, 'stop'), (second_reply, 'length'), (json.dumps({key: []}), 'stop')]
        calls, details = [], []
        async def provider(*args, **kwargs):
            calls.append({'max_tokens': kwargs.get('max_tokens')})
            if len(calls) > len(replies):
                raise AssertionError('unexpected fourth synthetic call')
            reply, reason = replies[len(calls)-1]
            return {'reply': reply, 'stop_reason': reason, 'usage': {'completion_tokens': 100}}
        garden = GardenComponent(model=None, max_capture_retries=1)
        if lane == 'capture':
            session = garden.capture_session(CaptureRequest(window='Synthetic conversation', locale='en'))
        else:
            cards = [{'id': f'card-{i}', 'summary': f'Fact {i}'} for i in range(15)]
            session = garden.maintenance_session(MaintenanceRequest(cards=cards, all_cards=cards,
                locale='en', known_ids=tuple(c['id'] for c in cards)))
        with patch.object(extraction.provider_client, 'reliable_chat_completion_async', provider), \
             patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            result = asyncio.run(extraction.extract(provider_config=object(), prompt='unused',
                parse=lambda _: (None, 'unused'), session=session, max_tokens=100,
                failure_detail_out=details.append))
        OBSERVATIONS.append({'lane': lane, 'first': first, 'empty': empty, 'calls': len(calls),
            'request_budgets': calls, 'result': result, 'failure_details': details})
        self.assertEqual(len(calls), 2, 'format/semantic and truncation share one retry budget')
        self.assertEqual(result, (None, 'output_truncated'))
        self.assertEqual(details, [{'stop_reason':'length', 'completion_tokens':100, 'max_tokens':100}])

    def test_capture_format_partial(self): self.case('capture', 'format', False)
    def test_capture_format_empty(self): self.case('capture', 'format', True)
    def test_capture_semantic_partial(self): self.case('capture', 'semantic', False)
    def test_capture_semantic_empty(self): self.case('capture', 'semantic', True)
    def test_dream_format_partial(self): self.case('dream', 'format', False)
    def test_dream_format_empty(self): self.case('dream', 'format', True)

