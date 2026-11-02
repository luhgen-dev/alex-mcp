"""Opt-in synthetic native-language replay through production ingress.

No real WhatsApp sends, physical HA actions or production records. Recognition
is separately tested by the voice unit fixtures and the owner's recordings.
"""
import argparse
from dataclasses import replace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'alex-mcp/app'))
import behavior_cert as b
from behavior_contracts import PromptContract, StateExpectation, ConversationContract, ConversationStep, _fs

p = argparse.ArgumentParser()
p.add_argument('--source-options', required=True)
p.add_argument('--report', required=True)
a = p.parse_args()
contracts = []
def case(name, domain, text, tools, seed='core', **kw):
    contracts.append(PromptContract('native.'+name, 'phase3', domain,
        'Synthetic current-language voice parity', (text,), _fs(*tools),
        sources=('text','voice'), seed=seed, **kw))
case('reminder.create','reminders','இன்னும் ஒரு அஞ்சு நிமிஷத்துல மருந்து எடுக்க ரிமைண்ட் பண்ணு', ['create_reminder'],seed='empty',
    state_expectations=(StateExpectation('reminders',where=(('source_message_id','$MID'),),
        fields=(('due_at_utc','2026-09-29T02:05:00+00:00'),('status','OPEN')),count=1,delta=1),))
case('reminder.list','reminders','எனக்கு என்னென்ன ரிமைண்டர் இருக்குன்னு காண்பிக்க முடியுமா?', ['list_reminders'],expected_terms=('Dentist',))
case('receipt.tamil','receipts','நான் கடைசியா கட்டிய மேனேஜ்மென்ட் கட்டணத்தோட ரிசீட் எனக்கு அனுப்பு', ['get_receipt','get_transaction_receipt'],expect_attachment=True,expect_attachment_of='management_receipt')
case('receipt.malay','receipts','Tolong hantar gambar resit bayaran management fee terakhir', ['get_receipt','get_transaction_receipt'],expect_attachment=True,expect_attachment_of='management_receipt')
case('report.pdf','reports','என்னோட செப்டம்பர் 2026 செலவுகள் PDFல அனுப்பு', ['report_export'],expect_attachment=True)
case('memory.browse','memory','நான் சேமிக்க சொன்னதை காட்டு', ['search_saved_items'],expected_terms=('Smoke-test code word',))
case('memory.picture','memory','நான் சேமித்த vinyl படத்தை எனக்கு அனுப்பு', ['get_saved_item'],expect_attachment=True,expect_attachment_of='vinyl')
case('shopping.read','shopping','என்னோட ஷாப்பிங் லிஸ்ட் ல என்ன இருக்கு?', ['list_shopping_items'],expected_terms=('Diapers','Bread'))
conversations = [ConversationContract('native.reminder.followup','phase3','reminders',
    'Native missing-time clarification remains grounded across voice turns','empty',(
    ConversationStep('நாளைக்கு மருந்து எடுக்க ரிமைண்ட் பண்ணு',_fs('create_reminder'),expect_clarification=True,unchanged_tables=('reminders',)),
    ConversationStep('காலை 9 மணிக்கு',_fs('create_reminder'),state_expectations=(
        StateExpectation('reminders',fields=(('due_at_utc','2026-09-30T01:00:00+00:00'),('status','OPEN')),count=1,delta=1),)),
    ),sources=('text','voice'))]
b.contracts_for_phase = lambda phase: contracts
b.conversations_for_phase = lambda phase: conversations
report = b.live_certify('phase3','auto',a.source_options,20000,a.report,0.5,
    live_adversarial_per_contract=0,live_strategy='full')
raise SystemExit(0 if report['status']=='PASS' else 1)
