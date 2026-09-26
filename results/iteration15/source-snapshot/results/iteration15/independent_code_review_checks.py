from copy import deepcopy
from pathlib import Path
from agent.coordinator.executor import PlannedCall
from agent.tools.car_tools import _navigation_plan_issue
checks=0
read=[PlannedCall('r','compute_route',{'destination':'P_CUBBON'})]
for utterance in [
 'Do not navigate to Cubbon Park.', "Don't navigate to Cubbon Park.",
 'Never navigate to Cubbon Park.', 'Please do not go to Cubbon Park.',
 'How do I navigate to Cubbon Park?', 'Can you explain how to navigate to Cubbon Park.',
 'If I navigate to Cubbon Park, tell me the distance.', 'Navigate to Cubbon Park?',
 'Navigate to Cubbon Park, but do not start navigation.',
 'Navigate to Cubbon Park and tell me the distance.',
 'Navigate to Cubbon Park; actually do not.', 'Navigate to', 'Navigate to the',
 'Navigate to charger.', 'Navigate to coffee.', 'Navigate to Seaview Dome.',
 'Navigate to Cubbon Park\nDo not start.', 'Cancel navigation.',
]:
 before=deepcopy(read)
 assert _navigation_plan_issue(utterance,True,read) is None, utterance
 assert read==before
 checks+=1
for prefix in ['Navigate to','Go to','Please drive to','Actually, take me to']:
 text=f'{prefix} Cubbon Park.'
 assert _navigation_plan_issue(text,True,read),text
 for dest in ['P_CUBBON','Cubbon Park']:
  calls=[PlannedCall('fresh','compute_route',{'destination':dest}),PlannedCall('start','start_navigation',{'route_id':'$fresh.route_id'})]
  assert _navigation_plan_issue(text,True,calls) is None
  for tool,args in [('cancel_navigation',{}),('add_waypoint',{'place_id':'P_HOME'}),('start_navigation',{'route_id':'$fresh.route_id'})]:
   assert _navigation_plan_issue(text,True,calls+[PlannedCall('extra',tool,args)])
   checks+=1
  checks+=1
 checks+=1
for path in ['place_id','name']:
 calls=[PlannedCall('s','search_destination',{'query':'Cubbon Park'}),PlannedCall('r','compute_route',{'destination':f'$s.places[0].{path}'}),PlannedCall('a','start_navigation',{'route_id':'$r.route_id'})]
 assert _navigation_plan_issue('Navigate to Cubbon Park.',True,calls) is None
 assert _navigation_plan_issue('Navigate to MG Road Metro Station.',True,calls)
 checks+=2
assert _navigation_plan_issue('Navigate to Cubbon Park.',False,read) is None
assert _navigation_plan_issue('Navigate to Cubbon Park.',True,[]) is None
checks+=2
for name in ['agent/coordinator/responder.py','agent/config.py','agent/config.yaml']:
 assert Path(name).read_bytes()==(Path('results/iteration14/source-snapshot')/name).read_bytes(),name
print(f'{checks} independent in-memory boundary checks passed; responder and config identical to iteration14. No models, services, or source edits.')
