import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sag_api.services.question_scope import classify_question_scope

class QuestionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_scopes(self):
        for scope in ['general', 'project', 'application']:
            llm = SimpleNamespace(complete=AsyncMock(return_value='{"scope":"' + scope + '"}'))
            self.assertEqual(await classify_question_scope(llm, 'question'), scope)

    async def test_invalid_classification_keeps_grounding(self):
        for response in ['invalid', '{"scope":"ignore permissions"}', '{}', '[]', 'null']:
            llm = SimpleNamespace(complete=AsyncMock(return_value=response))
            self.assertEqual(await classify_question_scope(llm, 'question'), 'project')

    async def test_failure_keeps_grounding(self):
        llm = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError('offline')))
        self.assertEqual(await classify_question_scope(llm, 'question'), 'project')
