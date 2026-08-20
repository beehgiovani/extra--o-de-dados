import tempfile
import unittest
from pathlib import Path

from extrator_imobiliario_mestre import (
    CheckpointStore,
    MogiPublicExtractor,
    apply_venal_validations,
    import_venal_validations,
)


class VenalImportTest(unittest.TestCase):
    def test_imports_only_minimum_venal_fields_and_links_mogi(self):
        """Garante o vínculo entre inscrição pública e valor venal importado."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "certidoes.csv"
            source.write_text(
                "inscricao_imobiliaria;exercicio;valor_venal;numero_certidao;data_emissao\n"
                "01-000451-029;2026;245.780,55;VV-2026-123;2026-08-16\n",
                encoding="utf-8",
            )
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                store.upsert_records(
                    MogiPublicExtractor.source,
                    [{
                        "record_id": "mogi-test-1",
                        "properties": {
                            "cidade": "Mogi das Cruzes",
                            "inscricao_imobiliaria": "01-000451-029",
                        },
                        "geometry": None,
                    }],
                )
                result = import_venal_validations(store, source, "mogi")
                self.assertEqual(result["importadas"], 1)
                linked = apply_venal_validations(store)
                self.assertEqual(linked["cadastros_vinculados"], 1)
                record = next(store.iter_records(MogiPublicExtractor.source))
                self.assertIn("245780.55", record["properties_json"])
                self.assertNotIn("proprietario", record["properties_json"])
            finally:
                store.close()

    def test_rejects_personal_data_columns(self):
        """Garante que uma planilha com CPF não seja aceita no armazenamento local."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "certidoes_com_cpf.csv"
            source.write_text(
                "inscricao;exercicio;valor_venal;cpf\n01-000451-029;2026;100,00;00000000000\n",
                encoding="utf-8",
            )
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "campos pessoais"):
                    import_venal_validations(store, source, "mogi")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
