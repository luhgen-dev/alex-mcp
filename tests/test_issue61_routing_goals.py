"""Issue61 exposed tools and immutable numbered goal identity, synthetic only."""
from dataclasses import replace
import asyncio
from unittest.mock import patch
from test_conversation_reliability import ConversationFixture
import test_core as core
import brain
import goal_references
from context import use_actor


class Issue61RoutingGoals(ConversationFixture):
    def test_domain_tools_survive_budget(self):
        cases = [
            ('What is my leave entitlement and remaining balance?', 'work_leave_balance'),
            ('Tell me the original instruction for that task', 'get_task_details'),
            ('Show our discussions', 'list_discussions'),
            ('Show our most recent Ear session', 'recall_discussion'),
            ('Activate the holiday savings goal', 'planning_lock_goal'),
            ('Update my goal target to RM900', 'planning_update_goal_target'),
        ]
        for text, tool in cases:
            with self.subTest(text=text):
                offered = brain._select_tool_names(text)
                self.assertIn(tool, offered)
                self.assertLessEqual(len(offered), brain.TOOL_EXPOSURE_MAX)
        self.assertNotIn('planning_create_goal', brain._select_tool_names('Update my goal target to RM900'))
        self.assertNotIn('search_saved_items', brain._select_tool_names('Show our most recent Ear session'))

    def test_frozen_goal_choice_updates_identity_not_new_row(self):
        actor = replace(self.actor('goal-create-one', 'Create a shared travel goal'), read_scope='family')
        with use_actor(actor):
            a = core.phase2_finance.create_goal('Synthetic travel', 700, 0, actor.phone, visibility='family', status='DRAFT')
            b = core.phase2_finance.create_goal('Synthetic repair', 300, 0, actor.phone, visibility='family', status='DRAFT')
            shown = goal_references.list_for_actor(actor)
        target = shown['goals'][1]['goal_id']
        update = replace(actor, source_message_id='goal-update',trusted_text='Update goal 2 target to RM450')
        with use_actor(update):
            result = core.mcp_server.planning_update_goal_target(450, update, choice=2, selection_id=shown['selection_id'])
            after = core.phase2_finance.list_goals(actor.phone)
        self.assertEqual(len(after),2)
        self.assertEqual(next(r for r in after if r['goal_id']==target)['target'],450)
        self.assertEqual({r['goal_id'] for r in after},{a['goal_id'],b['goal_id']})

    def test_goal_selection_cannot_cross_actor_or_chat(self):
        actor = replace(self.actor('goal-select-private', 'Create my private travel goal'),read_scope='private')
        with use_actor(actor):
            core.phase2_finance.create_goal('Synthetic private trip', 200, 0, actor.phone, status='DRAFT')
            shown = goal_references.list_for_actor(actor)
            foreign_chat = replace(actor, conversation_id='different-chat')
            with self.assertRaises(ValueError):
                goal_references.resolve(foreign_chat,choice=1,selection_id=shown['selection_id'])
        family = replace(actor, read_scope='family')
        with use_actor(family), self.assertRaises(PermissionError):
            goal_references.resolve(family,choice=1,selection_id=shown['selection_id'])

    def test_indirect_diary_cancellation_clarifies_without_writes(self):
        actor = self.actor('diary-indirect', "I think I won't be able to make it to the appointment")
        with patch.object(brain, '_call_mcp') as execute:
            reply, files = asyncio.run(brain.respond(actor, actor.trusted_text))
        execute.assert_not_called()
        self.assertIn('cancel', reply)
        self.assertIn('?', reply)
        self.assertEqual(files, [])
        add, blocked = brain._contextual_tool_hints('Cancel it', actor.trusted_text)
        self.assertIn('update_diary_event', add)

    def test_task_original_instruction_never_comes_from_current_question(self):
        evidence = [{'_tool_name':'get_task_details','title':'Synthetic repair',
                     'status':'OPEN','original_instruction':'Fix the shelf next weekend',
                     'history':[],'reminder':None}]
        answer = brain._task_detail_response('You asked what are my tasks', evidence, 'original task instruction')
        self.assertIn('Fix the shelf next weekend', answer)
        self.assertNotIn('what are my tasks', answer)
        evidence[0]['original_instruction'] = None
        self.assertIn('Not available', brain._task_detail_response('invented',evidence,'original task instruction'))

    def test_goal_direct_id_rechecks_effective_actor_not_ambient_scope(self):
        private = replace(self.actor('direct-private-goal', 'private goal'), read_scope='private')
        with use_actor(private):
            row = core.phase2_finance.create_goal('Hidden synthetic goal', 210, 0, private.phone, status='DRAFT')
        family = replace(private, read_scope='family', trusted_text='Activate this shared goal')
        # Even with stale private ambient context, the passed authenticated scope wins.
        with use_actor(private), self.assertRaisesRegex(Exception, "unavailable in the current authorized scope"):
            core.mcp_server.planning_lock_goal(family, goal_id=row['goal_id'])
        with use_actor(family):
            shown = goal_references.list_for_actor(private)
        self.assertIn(row['goal_id'], [goal['goal_id'] for goal in shown['goals']])
