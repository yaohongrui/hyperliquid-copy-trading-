import argparse
from config.loader import load

def validate(path):
 c=load(path); assert isinstance(c,dict); assert c.get('leaders') is not None; assert c.get('execution_accounts') is not None
 names={a['name'] for a in c['leaders']}; accounts=set(c['execution_accounts'])
 for a in c['leaders']:
  assert a.get('execution_account') in accounts, f"unknown execution account: {a.get('execution_account')}"
 return True
if __name__=='__main__':
 p=argparse.ArgumentParser(); p.add_argument('config'); validate(p.parse_args().config); print('valid')
