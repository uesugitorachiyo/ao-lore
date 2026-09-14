"""Additional semantic/schema, encoding and boundary regressions; synthetic data only."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from ao_lore.source_viewer.contracts import ViewerError, digest, validate_resolve
from ao_lore.source_viewer.demo import demo_material
from ao_lore.source_viewer.formats import docx_projection
from ao_lore.source_viewer.server import ViewerState
from ao_lore.source_viewer.store import SnapshotStore, provision_snapshot

try:
    import jsonschema
except ImportError:
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]


class TestAdditionalBoundaryChecks(unittest.TestCase):
    def test_utf16_dtd_cannot_bypass_xml_guard(self):
        xml = '<!DOCTYPE x [<!ENTITY e "not permitted">]><x>&e;</x>'.encode('utf-16')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('[Content_Types].xml', 'types')
            archive.writestr('word/document.xml', xml)
        with self.assertRaises(ViewerError): docx_projection(output.getvalue())

    def test_maximum_length_grant_identifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary); manifest, originals = demo_material()
            identity = 'g' * 128
            provision_snapshot(home, manifest, originals, grant_id=identity)
            self.assertEqual(len(SnapshotStore(home, identity).list_records()), 3)

    def test_colliding_bare_ids_remain_origin_scoped(self):
        manifest, originals = demo_material()
        other = copy.deepcopy(manifest['records'][0])
        other['evidence']['workspace_id'] = 'demo-reference'
        manifest['records'].append(other)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            provision_snapshot(home, manifest, originals, grant_id='origin-test')
            store = SnapshotStore(home, 'origin-test')
            for record in (manifest['records'][0], other):
                evidence = record['evidence']
                returned = store.get_record(tuple(evidence[k] for k in ('workspace_id','generation_digest','evidence_id')))
                self.assertEqual(returned['evidence']['workspace_id'], evidence['workspace_id'])

    def test_unknown_grant_does_not_create_runtime_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with self.assertRaises(ViewerError): SnapshotStore(home, 'not-retained')
            self.assertEqual(list(home.iterdir()), [])


@unittest.skipUnless(jsonschema is not None, 'optional JSON Schema checker is not installed')
class TestPublishedViewerSchemas(unittest.TestCase):
    def schema(self, kind):
        return json.loads((ROOT / 'schemas' / 'ao-lore' / ('source-viewer-'+kind+'-v0.1.schema.json')).read_text())

    def test_closed_binding_schema(self):
        manifest, _ = demo_material()
        jsonschema.Draft202012Validator(self.schema('bindings')).validate(manifest)
        manifest['path'] = '/private/source'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(self.schema('bindings')).validate(manifest)

    def test_grant_schema_and_explicit_whole_original_scope(self):
        manifest, originals = demo_material()
        with tempfile.TemporaryDirectory() as temporary:
            grant = provision_snapshot(Path(temporary), manifest, originals, grant_id='schema-test')
        validator = jsonschema.Draft202012Validator(self.schema('grant'))
        validator.validate(grant)
        grant['authorized_scope'] = 'evidence-block-only'
        with self.assertRaises(jsonschema.ValidationError): validator.validate(grant)

    def test_resolve_schema_matches_real_text_and_docx_output(self):
        manifest, originals = demo_material()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary); provision_snapshot(home,manifest,originals,grant_id='schema-test')
            state = ViewerState(SnapshotStore(home,'schema-test'))
            token = state.login(state.launch_code); session = state.authenticate('Bearer '+token)
            for record in manifest['records'][1:]:
                key = {k:record['evidence'][k] for k in ('workspace_id','generation_digest','evidence_id')}
                result = state.resolve(session,key)
                jsonschema.Draft202012Validator(self.schema('resolve')).validate(result)
                validate_resolve(result)
                result['native_binding_revalidated'] = True
                with self.assertRaises(ViewerError): validate_resolve(result)
