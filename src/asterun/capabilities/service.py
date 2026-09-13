"""固定计划、标准事务准入与 QualityRuntime 的窄桥。"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
from asterun import control_protocol as protocol
from asterun.connections.admission import input_digest
from asterun.control import identifier,now,instant,reject,TERMINAL
from asterun.contracts import RunStatus,TERMINAL_RUN_STATUSES,AcceptanceStatus,OperationStatus
from asterun.ids import RunId,TaskId
from asterun.policy import enforce
from asterun.quality import snapshot

PROFILE='asterun-capability/v1'
WORKFLOWS=('wf_capability_call','wf_code_change')

class CapabilityRuntime:
    def __init__(self,app):self.app=app
    @property
    def control(self):return self.app.control

    def authorize(self,workspace,method):
        enforce(self.app.policy.authorize(self.app.principal,method,workspace))
        self.app._require_grant(workspace)
        if self.app.admission is None:reject('CONFIG_REQUIRED','能力 profile 需要显式 v2 配置及 SQLite')
        self.app.admission.persistent()

    def handle(self,method,payload):
        if method=='capability.discovery':
            enforce(self.app.policy.authorize(self.app.principal,'control.read','*'))
            from importlib.resources import files
            raw=files('asterun.schemas').joinpath('asterun-capability-v1.schema.json').read_bytes()
            import hashlib
            return {'schema_version':PROFILE,'namespace_id':self.control.namespace,'principal_id':self.control.principal,
                    'schema_sha256':hashlib.sha256(raw).hexdigest(),'methods':['capability.prepare','capability.submit','capability.get'],
                    'workflows':[{'id':w,'revision':1} for w in WORKFLOWS],
                    'validators':['val_execution','val_output','val_code_quality'],
                    'requires':'explicit_v2_sqlite','rcv1_schema_sha256':protocol.SCHEMA_SHA256}
        if payload['schema_version']!=PROFILE or payload['namespace_id']!=self.control.namespace:
            reject('VERSION_UNSUPPORTED','能力 profile 或命名空间不匹配')
        if method=='capability.get':
            self.control.get('Run',payload['run_id'])  # 先授权，再推进本机已提交 outbox。
            self.app.poll()
            return self.get(payload['run_id'])
        if method=='capability.prepare':return self.prepare(payload)
        return self.submit(payload)

    def receipt(self,method,payload,workspace,create):
        c=self.control;self.authorize(workspace,method)
        key=payload['idempotency_key'];domain=PROFILE+':'+method
        sha=protocol.digest(payload)
        with c.store.transaction():
            old=c.store.find_operation(c.namespace,c.principal,domain,key)
            if old:
                if old['payload_sha256']!=sha:reject('IDEMPOTENCY_CONFLICT','同 profile/方法/命名空间/主体的原键输入不同')
                return deepcopy(old['extensions']['asterun.response'])
            operation=c.entity('Operation','opr',task_id=None,request_id=identifier('req'),method=domain,
                principal_id=c.principal,idempotency_key=key,payload_sha256=sha,status='SUCCEEDED',result_ref=None,error_code=None)
            response=create(operation)
            operation['extensions']['asterun.response']=response
            c.save(operation);c.store.save_operation(operation,c.principal,key,sha)
        return response

    def prepare(self,payload):
        app,c=self.app,self.control;workspace=payload['workspace']
        def create(operation):
            app._require_applied_config()
            backend=app._backend(payload['backend']);cap=payload['capability'];wf=payload['workflow_id']
            from asterun.plugins.builtins import build_registry
            registration=build_registry(app.config).get(app.config.connections[backend.config.connection_ref]['plugin_id'])
            data=registration.manifest.validate_input(cap,payload['input'])
            typed=hasattr(backend,'dispatch_capability')
            if not typed and cap!='agent.execute':reject('CAPABILITY_UNSUPPORTED','内置兼容后端只接线 agent.execute')
            quality=payload.get('quality');baseline=None
            if wf=='wf_code_change':
                if cap!='agent.execute' or set(data)!={'text'}:reject('CAPABILITY_UNSUPPORTED','代码工作流需要 agent.execute 文本输入')
                # 质量引擎的实现/修复均使用既有 Agent 路径，结构化工具调用不伪装代码执行。
                typed=False
                if not quality:reject('INVALID_REQUEST','代码工作流需要确定性检查、目标范围和有限修复次数')
                self.authorize(workspace,'workflow.start')
                if quality['max_repairs']>app.config.workflow.max_repairs:reject('BUDGET_EXHAUSTED','请求修复次数高于配置上限')
                if not any(check['kind'] in {'file_exists','file_contains'} for check in quality['checks']):
                    reject('INVALID_REQUEST','代码质量必须包含工作区文件的确定性检查')
                for check in quality['checks']:
                    if check['kind'] in {'contains','file_contains'} and not check.get('text'):
                        reject('INVALID_REQUEST','内容检查必须提供非空的预期文本')
                    if check['kind'] in {'file_exists','file_contains'} and check.get('path') not in quality['target_paths']:
                        reject('INVALID_REQUEST','检查文件超出验收快照范围')
                baseline,_=snapshot(app.config.get_workspace(workspace).root,quality['target_paths'],state_dir=app.state_dir)
            elif quality is not None:reject('INVALID_REQUEST','通用调用不能附加未接线的代码质量语义')
            turns=2+2*quality['max_repairs'] if quality else 1
            if turns>app.config.scheduler.max_runs_per_task:reject('BUDGET_EXHAUSTED','工作流最坏轮次超出任务运行上限')
            binding=app.admission.prepare(workspace,backend.config.name,input_digest(data.get('text',''),
                capability=cap if typed else None,capability_input=data if typed else None),capability=cap)
            plan={'id':operation['id'],'schema_version':PROFILE,'namespace_id':c.namespace,'principal_id':c.principal,
                'workspace':workspace,'backend':backend.config.name,'capability':cap,'input':data,'typed':typed,
                'workflow_id':wf,'workflow_revision':1,'quality':quality,'baseline':baseline,'max_turns':turns,
                'configuration_sha256':c.configuration_sha256,'admission_plan_id':binding['id'],
                'binding_sha256':binding['binding_sha256'],'reservations':binding['reservations'],
                'connection_summary':{key:binding['binding'][key] for key in ('connection_ref','provider_account_ref','plugin_id','plugin_version','workspace_root')},
                'runtime_summary':{key:value for key,value in binding['binding']['connection'].items() if key in {'auth_mode','upstream_version','billing_pool_refs'}},
                'model':binding['binding']['connection'].get('options',{}).get('model'),
                'review_backend':app.config.workflow.review_backend if quality else None,
                'approved_substitute':app.config.workflow.approved_substitute if quality else None,
                'effect_class':registration.manifest.capability(cap)['effect_class'],
                'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat().replace('+00:00','Z')}
            plan['plan_sha256']=protocol.digest(plan)
            operation['extensions']['asterun.capability_plan']=plan
            operation['result_ref']=plan['id']
            return {'schema_version':PROFILE,'prepared':True,'plan':plan,'provider_called':False,
                    'budget_reserved':False,'account_quota_verified':False}
        return self.receipt('capability.prepare',payload,workspace,create)

    def load(self,plan_id):
        c=self.control;operation=c.store.get('Operation',plan_id,c.namespace)
        if operation['principal_id']!=c.principal or operation['method']!=PROFILE+':capability.prepare':
            reject('FORBIDDEN','计划不属于当前 profile 或认证主体')
        plan=deepcopy(operation['extensions'].get('asterun.capability_plan',{}));sha=plan.pop('plan_sha256',None)
        if protocol.digest(plan)!=sha:reject('BINDING_MISMATCH','计划持久摘要不一致')
        plan['plan_sha256']=sha
        return plan

    def assert_plan(self,plan):
        app=self.app;c=self.control
        if instant(plan['expires_at'])<=instant(now()):reject('REVISION_CONFLICT','准备计划已过期，请重新确认')
        if plan['configuration_sha256']!=c.configuration_sha256:reject('BINDING_MISMATCH','计划的配置或身份已变化')
        app.admission.assert_plan(app.admission.store.get_plan(plan['admission_plan_id'],app.principal.subject_id))
        if plan['baseline'] is not None:
            current,_=snapshot(app.config.get_workspace(plan['workspace']).root,plan['quality']['target_paths'],state_dir=app.state_dir)
            if current!=plan['baseline']:reject('BINDING_MISMATCH','代码目标基线已变化，请重新准备')

    def submit(self,payload):
        c=self.control;plan=self.load(payload['plan_id'])
        def create(operation):
            if payload['plan_sha256']!=plan['plan_sha256']:reject('BINDING_MISMATCH','提交未确认相同计划摘要')
            self.assert_plan(plan)
            scope={'resource_id':identifier('rsc',plan['workspace']),'operations':['agent.execute'],'selector':None}
            params={'title':plan['capability'],'objective':'执行已确认计划 '+plan['id']+'；完整输入及摘要见准备记录','bot_instance_id':None,
                    'resource_scopes':[scope],'constraints':[],
                    'acceptance':[{'id':'acc_execution','validator_id':'val_execution','input_artifact_ids':[],
                                   'required':True,'subject_revision_sha256':None}],
                    'budget_limits':[{'meter':'turns','limit':plan['max_turns'],'enforcement':'hard','billing_pool_ref':None}]}
            command={'expected_revision':0,'params':params,'expires_at':plan['expires_at']}
            task=c._task_create(command,operation)
            command.update(expected_revision=task['revision'],method='run.start',params={'task_id':task['id'],
                'workflow_id':plan['workflow_id'],'workflow_revision':1,'configuration_sha256':plan['configuration_sha256']})
            run=c._start(command,operation,retry=False,profile_plan=plan)
            operation['task_id']=task['id'];operation['result_ref']=run['id']
            return {'schema_version':PROFILE,'status':'ACCEPTED','task_id':task['id'],'run_id':run['id'],
                    'operation_id':operation['id'],'plan_sha256':plan['plan_sha256'],'quality_verdict':'PENDING' if plan['quality'] else 'NOT_APPLICABLE'}
        return self.receipt('capability.submit',payload,plan['workspace'],create)

    def get(self,run_id):
        c=self.control;run=c.get('Run',run_id);plan=run['extensions'].get('asterun.capability_plan')
        if not plan:reject('CAPABILITY_UNSUPPORTED','该运行不属于能力 profile')
        result={'schema_version':PROFILE,'run':run,'quality_verdict':'NOT_APPLICABLE'}
        if plan['quality']:
            action=next(a for a in c.store.list('Action',c.namespace) if a['run_id']==run_id and not a['extensions'].get('asterun.quality_step'))
            legacy=c._find_legacy(action)
            result['quality_verdict']='PENDING'
            if legacy and legacy.quality:
                self.app.quality.validate_evidence(legacy);legacy=self.app.store.get_task(legacy.id)
                result['quality']=deepcopy(legacy.quality)
                result['quality_verdict']='PASS' if legacy.acceptance==AcceptanceStatus.PASSED and not legacy.evidence_stale else 'STALE' if legacy.evidence_stale else 'PENDING'
        return result

    def admit_quality_intent(self,parent,operation,legacy,legacy_run):
        c=self.control;run=c.store.get('Run',parent['run_id'],c.namespace)
        if run['workflow_id']!='wf_code_change':reject('CAPABILITY_UNSUPPORTED','原 wf_execute 不能派生质量轮次')
        plan=run['extensions']['asterun.capability_plan']
        if legacy.repair_count>plan['quality']['max_repairs']:reject('BUDGET_EXHAUSTED','已达到本计划修复上限')
        c._own(parent,run)
        c._dispatch_guard(parent,run,already_admitted=True)
        payload={'workspace':legacy.workspace,'backend':legacy_run.backend.value,'text':legacy_run.prompt or legacy.text,
                 'expected_revision':self.app.config.revision,'idempotency_key':operation.idempotency_key}
        std_operation=c.entity('Operation','opr',task_id=run['task_id'],request_id=identifier('req'),method=PROFILE+':quality.'+legacy_run.role,
            principal_id=c.principal,idempotency_key=identifier('idem',operation.idempotency_key),payload_sha256=protocol.digest(payload),
            status='SUCCEEDED',result_ref=None,error_code=None)
        task=c.task(run['task_id'],'workflow.start')
        action=c._admit(task,run,std_operation,{'expires_at':plan['expires_at']},payload_override=payload,role=legacy_run.role)
        action['extensions'].update({'asterun.legacy_task_id':legacy.id.value,'asterun.legacy_run_id':legacy_run.id.value})
        c.save(action,update=True);std_operation['result_ref']=action['id'];c.save(std_operation)
        c.store.save_operation(std_operation,c.principal,std_operation['idempotency_key'],std_operation['payload_sha256'])
        return action

    def quality_action_for_operation(self,operation):
        if self.app.admission is None or not getattr(operation,'run_id',None):return None
        binding=self.app.admission.store.binding_for_run(operation.run_id.value,self.control.namespace,self.app.principal.subject_id)
        if not binding or not binding.get('control_action_id'):return None
        action=self.control.store.get('Action',binding['control_action_id'],self.control.namespace)
        return action if action['extensions'].get('asterun.quality_step') else None

    def quality_control_for_operation(self,operation):
        action=self.quality_action_for_operation(operation)
        return self.control.store.get('Run',action['run_id'],self.control.namespace) if action else None

    def guard_quality_dispatch(self,legacy,operation):
        action=self.quality_action_for_operation(operation)
        if not action:return False
        c=self.control
        with c.store.transaction():
            run=c.store.get('Run',action['run_id'],c.namespace);c._own(action,run)
            c._dispatch_guard(action,run)
            grant=c.store.get('Grant',action['grant_ids'][0],c.namespace);grant['used_count']+=1;c.save(grant,update=True)
            action['dispatched_at']=now();c.transition(action,'EXECUTING');c.store.mark_delivery(action['id'],c.namespace)
            assignment=c.store.get('Assignment',action['assignment_id'],c.namespace);c.transition(assignment,'RUNNING')
        return True

    def settle_quality_steps(self,run):
        c=self.control
        for action in c.store.list('Action',c.namespace):
            if action['run_id']!=run['id'] or not action['extensions'].get('asterun.quality_step') or action['status'] in {'SUCCEEDED','FAILED','DENIED','CANCELED'}:continue
            legacy_run=self.app.store.get_run(RunId(action['extensions']['asterun.legacy_run_id']))
            with c.store.transaction():
                c._own(action,run)
                c._bind_session(action,run,self.app.store.get_task(legacy_run.task_id),legacy_run)
                assignment=c.store.get('Assignment',action['assignment_id'],c.namespace)
                if legacy_run.status==RunStatus.PENDING_RECONCILE:
                    c.transition(action,'UNKNOWN_REMOTE');self.app.admission.control_uncertain(action['id']);continue
                if legacy_run.status not in TERMINAL_RUN_STATUSES or not legacy_run.terminated:continue
                evidence=c._evidence(run['task_id'],action['action_sha256'],'质量子轮次终态回执，不代表质量通过',run_id=run['id'],action_id=action['id'],validator='val_receipt')
                action['receipt_evidence_ids']=[evidence['id']]
                if action['status']=='ADMITTED':
                    c.transition(action,'CANCELED');c.transition(assignment,'CANCELED');amount=0
                else:
                    c.transition(action,'VERIFYING');c.transition(action,'SUCCEEDED' if legacy_run.status==RunStatus.SUCCEEDED else 'FAILED')
                    c.transition(assignment,'VERIFYING');c.transition(assignment,'SUCCEEDED' if legacy_run.status==RunStatus.SUCCEEDED else 'FAILED');amount=1
                c._settle_reservation(action,amount,[evidence['id']]);c.store.finish_delivery(action['id'],c.namespace)

    def quality_progress(self,action,run,legacy,implementation):
        c=self.control;app=self.app;plan=run['extensions']['asterun.capability_plan']
        if implementation.status not in TERMINAL_RUN_STATUSES:return False
        if implementation.status!=RunStatus.SUCCEEDED:return False
        self.settle_quality_steps(run)
        run.update(c.store.get('Run',run['id'],c.namespace))
        legacy=app.store.get_task(legacy.id)
        current=app.store.get_run(legacy.current_run_id)
        if legacy.quality and current.status==RunStatus.PENDING_RECONCILE:
            from asterun.errors import AsterunError
            try:app.task_reconcile({'task_id':legacy.id.value})
            except AsterunError:pass
            current=app.store.get_run(legacy.current_run_id)
        if run['desired_state']!='RUNNING':
            if legacy.quality and not run['extensions'].get('asterun.quality_control_paused'):
                # 控制暂停只停止编排；不把未知/活动原生轮次改写成已暂停。
                app.store.save_task(replace(legacy,paused=True,orchestration_owner='human'))
                run['extensions']['asterun.quality_control_paused']=True;c.save(run,update=True)
            if current.status not in TERMINAL_RUN_STATUSES:
                if run['desired_state']=='CANCELED' and not current.cancel_requested:app.task_cancel(legacy.id.value)
                return True
            return False
        if run['extensions'].pop('asterun.quality_control_paused',False):
            app.task_handoff({'task_id':legacy.id.value,'action':'resume'})
            c.save(run,update=True);legacy=app.store.get_task(legacy.id)
        if not legacy.quality:
            payload={'task_id':legacy.id.value,'idempotency_key':'w1_'+run['id'],
                     'target_paths':plan['quality']['target_paths'],'checks':plan['quality']['checks'],
                     'auto_repair':plan['quality']['max_repairs']>0}
            try:app.quality.start(payload)
            except Exception as error:
                from asterun.errors import AsterunError
                if not isinstance(error,(AsterunError,OSError)):raise
                # 实现已完成但无法建立验收时保留 WAITING，不伪造 PASS。
                run['extensions']['asterun.quality_wait_reason']=getattr(error,'code','INVALID_REQUEST')
                c.save(run,update=True)
        run.update(c.store.get('Run',run['id'],c.namespace))
        legacy=app.store.get_task(legacy.id)
        app.quality.validate_evidence(legacy);legacy=app.store.get_task(legacy.id)
        if legacy.quality.get('phase')=='completed' and legacy.acceptance==AcceptanceStatus.PASSED and not legacy.evidence_stale:
            self.settle_quality_steps(run)
            run['extensions']['asterun.quality_result']={'target':legacy.quality['target'],'checks':legacy.quality['check_results'],
                'reviews':legacy.quality['reviews'],'closures':legacy.quality['closures'],'implementation_run_id':legacy.quality['implementation_run_id'],
                'baseline':plan['baseline'],'plan_sha256':plan['plan_sha256']}
            c.save(run,update=True);return False
        if run['status'] not in {'WAITING','PAUSING','PAUSED','CANCELING'}:c.transition(run,'WAITING',reason='APPROVAL_REQUIRED')
        return True

    def quality_evidence(self,task,run,action):
        c=self.control;value=run['extensions'].get('asterun.quality_result')
        if not value:reject('EVIDENCE_STALE','代码工作流缺少绑定质量证据')
        artifact=c._artifact(task['id'],json.dumps(value,ensure_ascii=False).encode(),run_id=run['id'],artifact_type='document')
        return c._evidence(task['id'],protocol.digest(value),'确定性检查及独立审查通过，仅针对记录的目标快照；账户独立性仍未验证',
            run_id=run['id'],action_id=action['id'],validator='val_code_quality',artifacts=[artifact['id']])
