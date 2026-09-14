"""Employee enrichment must remain descriptive and preserve opaque credentials."""
import copy
import json
import unittest
import test_worker as fixtures
w = fixtures.w

class EmployeeIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkerLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config, self.api, self.alice = fixture.config, fixture.api, fixture.alice
        self.run_worker = fixture.run_worker
        self.config['employee_profile']={'enabled':True,'catalog_lookup':False}

    def test_profile_projection_never_elevates_roles_or_ranking(self):
        self.api.source_users['od_platform'][0].update(name='Synthetic Employee',mobile='+12025550123',email='person@example.test',job_title='CEO',job_level_id='level1',is_tenant_manager=True)
        self.alice['ranking']=42
        self.run_worker()
        props=self.alice['properties']
        self.assertEqual(props['feishu_mobile'],'+12025550123')
        self.assertEqual(props['feishu_job_title'],'CEO')
        self.assertEqual(self.alice['ranking'],42)
        self.assertEqual(props['headplane_role'],'network_admin')  # Comes only from fixture's explicit group map.
        self.config['headplane_role_groups']={}
        self.run_worker()
        self.assertEqual(self.alice['properties']['headplane_role'],'member')
        departments=json.loads(self.alice['properties']['feishu_departments'])
        self.assertEqual(departments[0]['id'],'od_platform')
        self.assertEqual([d['id'] for d in departments[0]['path']],['od_engineering','od_platform'])

    def test_masked_oauth_properties_are_not_written_back(self):
        self.alice['properties']['oauth_lark_accessToken']='***'
        def unmask(user):
            user['properties']['oauth_lark_accessToken']='synthetic-real-token'
        self.api.before_update=unmask
        self.run_worker()
        self.assertEqual(self.alice['properties']['oauth_lark_accessToken'],'synthetic-real-token')

    def test_unavailable_phone_does_not_erase_native_value(self):
        self.alice['phone']='+12025550123'
        self.api.native_columns.append({'name':'Phone','casdoorName':'Phone','isHashed':True})
        with self.assertRaisesRegex(w.SyncError,'refusing to erase'):
            self.run_worker()
        self.assertEqual(self.api.writes,[])

    def test_audit_allows_missing_status_but_cannot_apply(self):
        self.api.source_users['od_platform'][0].pop('status')
        snapshot=w.Feishu(self.config['feishu'], self.api).snapshot(audit=True)
        self.assertIsNone(snapshot['users']['ou_alice']['status'])
        with self.assertRaisesRegex(w.SyncError,'audit-only'):
            w.plan(self.config,snapshot,self.api.users,self.api.groups,{})
        self.assertEqual(self.api.writes,[])

    def test_audit_report_has_counts_without_employee_values(self):
        self.api.source_users['od_platform'][0].update(name='Never Print This',mobile='+12025550123')
        report=w.audit_directory(self.config,self.api)
        self.assertEqual(report['profile_coverage']['fields']['mobile']['available'],1)
        self.assertNotIn('+12025550123',json.dumps(report))
        self.assertNotIn('Never Print This',json.dumps(report))
        self.assertEqual(self.api.writes,[])

    def test_company_mismatch_stops_before_casdoor_access(self):
        self.config['feishu']['expected_tenant_name']='Expected Company'
        from unittest.mock import patch
        with patch.object(w.Feishu,'tenant',return_value='Different Company'):
            with self.assertRaisesRegex(w.SyncError,'company name'):
                self.run_worker()
        self.assertEqual(self.api.writes,[])

    def test_profile_only_works_without_status_and_preserves_access(self):
        self.api.source_users['od_platform'][0].pop('status')
        self.api.source_users['od_platform'][0].update(name='Synthetic Employee', job_title='CEO')
        original_groups=copy.deepcopy(self.alice['groups'])
        original_status=self.alice['isForbidden']
        self.alice['properties']['headplane_role']='viewer'
        result=w.profile_only(self.config,apply=True,http=self.api)
        self.assertEqual(result['mode'],'profile-only-apply')
        self.assertEqual(self.alice['properties']['feishu_job_title'],'CEO')
        self.assertEqual(self.alice['properties']['headplane_role'],'viewer')
        self.assertEqual(self.alice['groups'],original_groups)
        self.assertEqual(self.alice['isForbidden'],original_status)
        self.assertNotIn('run-syncer',self.api.writes)
        self.assertTrue(all(action=='update-user' for action in self.api.writes))
        from pathlib import Path
        self.assertFalse(Path(self.config['state_file']).exists())
        again=w.profile_only(self.config,apply=True,http=self.api)
        self.assertEqual(again['updates'],0)

    def test_profile_only_dry_run_never_writes(self):
        result=w.profile_only(self.config,http=self.api)
        self.assertEqual(result['mode'],'profile-only-dry-run')
        self.assertEqual(self.api.writes,[])
