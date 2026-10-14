import hashlib
import json
from pathlib import Path

import yaml

from ninja_pce.cli import main
from ninja_pce.state import digest

PROFILE = Path(__file__).resolve().parents[1] / 'gns3/lab-data/iosxe-16.9.7/multi-area-profile.yml'


def test_lab_render_binds_reviewable_configs_and_is_deterministic(tmp_path, capsys):
    output = tmp_path / 'rendered'
    args = ['lab-render', '--profile', str(PROFILE), '--output-dir', str(output)]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan == json.loads((output / 'plan.json').read_text())
    assert plan['plan_id'] == digest({key: value for key, value in plan.items() if key != 'plan_id'})
    assert plan['dry_run'] and not plan['authoritative']
    assert set(plan['configuration_sha256']) == {'P2', 'P4', 'P5', 'PE4'}
    for node, expected in plan['configuration_sha256'].items():
        assert hashlib.sha256((output / (node + '.cfg')).read_bytes()).hexdigest() == expected
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == plan


def test_lab_render_invalid_profile_writes_nothing(tmp_path, capsys):
    source = tmp_path / 'bad.json'
    source.write_text('{"schema_version": 1, "platform_profile": "unqualified"}')
    output = tmp_path / 'absent'
    assert main(['lab-render', '--profile', str(source), '--output-dir', str(output)]) == 1
    assert not output.exists()
    assert 'ERROR:' in capsys.readouterr().err


def test_lab_render_generates_explicit_domain_policy_and_clears_it_when_not_selected(tmp_path, capsys):
    output = tmp_path / 'rendered'
    args = ['lab-render', '--profile', str(PROFILE), '--output-dir', str(output)]
    assert main([*args, '--bgpls-asn', '65000', '--bgpls-instance-id', '0', '--bgpls-identifier', '0']) == 0
    plan = json.loads(capsys.readouterr().out)
    policy = json.loads((output / 'ospf-multi-area-policy.json').read_text())
    assert plan['ospf_multi_area_policy_digest'] == digest(policy)
    assert {item['node_id'] for item in policy['bindings']} == {'P2', 'P4'}
    assert plan['node_map_digest'] == digest(json.loads((output / 'node-map.json').read_text()))
    assert main(args) == 0
    assert not (output / 'ospf-multi-area-policy.json').exists()
    assert 'ospf_multi_area_policy_digest' not in json.loads(capsys.readouterr().out)


def test_lab_render_partial_domain_policy_fails_before_writing(tmp_path, capsys):
    output = tmp_path / 'absent'
    assert main(['lab-render', '--profile', str(PROFILE), '--output-dir', str(output), '--bgpls-asn', '65000']) == 1
    assert not output.exists()
    assert 'together' in capsys.readouterr().err


def test_lab_render_cannot_delete_its_input_when_clearing_a_policy(tmp_path, capsys):
    source = tmp_path / 'ospf-multi-area-policy.json'
    original = json.dumps(yaml.safe_load(PROFILE.read_text())).encode()
    source.write_bytes(original)
    assert main(['lab-render', '--profile', str(source), '--output-dir', str(tmp_path)]) == 1
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]
    assert 'must be distinct' in capsys.readouterr().err
