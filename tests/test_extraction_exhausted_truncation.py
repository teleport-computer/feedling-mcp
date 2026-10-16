"""Do not replace known provider truncation with a downstream format error."""
import asyncio,json
import pytest
from memgarden import GardenComponent,CaptureRequest,MaintenanceRequest
from model_api_runtime.v2 import extraction

@pytest.mark.parametrize('lane',['capture','dream'])
@pytest.mark.parametrize('reply_kind',['empty','incomplete','valid_empty','valid_nonempty'])
def test_exhausted_session_retains_last_truncation_cause(monkeypatch,lane,reply_kind):
    key='cards' if lane=='capture' else 'consolidations'
    reply='' if reply_kind=='empty' else '{"'+key+'":[' if reply_kind=='incomplete' else '{"'+key+'":[]}'
    if reply_kind=='valid_nonempty':
        card={'bucket':'Facts','summary':'Prefers tea','content':'The user prefers tea to coffee.','importance':0.6,'pulse':0.2}
        item={'action':'add',**card} if lane=='capture' else {'op':'merge','card_ids':['card-0','card-1'],'rationale':'same preference','result':card}
        reply=json.dumps({key:[item]})
    calls=[];details=[]
    async def provider(*a,**kw):
        calls.append(1)
        return {'reply':reply,'stop_reason':'length','usage':{'completion_tokens':100}}
    monkeypatch.setattr(extraction.provider_client,'reliable_chat_completion_async',provider)
    garden=GardenComponent(model=None,max_capture_retries=0)
    if lane=='capture':session=garden.capture_session(CaptureRequest(window='Synthetic conversation',locale='en'))
    else:
        cards=[{'id':f'card-{i}','summary':f'Fact {i}'} for i in range(15)]
        session=garden.maintenance_session(MaintenanceRequest(cards=cards,all_cards=cards,locale='en',known_ids=tuple(c['id'] for c in cards)))
    result=asyncio.run(extraction.extract(provider_config=object(),prompt='unused',parse=lambda _:(None,'unused'),session=session,max_tokens=100,failure_detail_out=details.append))
    assert calls==[1]
    if reply_kind=='valid_nonempty':
        assert result[1] is None and len(result[0])==1 and details==[]
        assert (result[0][0]['content'] if lane=='capture' else result[0][0]['result']['content'])=='The user prefers tea to coffee.'
    elif reply_kind=='valid_empty':
        assert result==([],None) and details==[]
    else:
        assert result==(None,'output_truncated')
        assert details==[{'stop_reason':'length','completion_tokens':100,'max_tokens':100}]
