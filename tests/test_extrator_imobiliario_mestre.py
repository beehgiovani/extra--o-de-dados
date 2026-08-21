import tempfile
import unittest
from pathlib import Path

from extrator_imobiliario_mestre import (
    CheckpointStore,
    MogiItbiVenalExtractor,
    MogiIptuVenalExtractor,
    MogiPublicExtractor,
    apply_fiscal_status,
    apply_mogi_iptu_venal,
    apply_venal_validations,
    export_city_results,
    import_fiscal_status,
    import_venal_validations,
    logradouro_key,
    point_in_geometry,
)


class VenalImportTest(unittest.TestCase):
    def test_keeps_cadastro_id_separate_from_shared_id_local(self):
        """Não transforma duas unidades do mesmo id_local em um único cadastro."""
        first = MogiPublicExtractor.clean_row(
            {
                "cadastro_id": "000.002",
                "id_local": "01-004811-001",
                "nro_local": "00002",
                "complemento": "unid.001",
                "area1": "24,79",
            },
            2024,
        )
        second = MogiPublicExtractor.clean_row(
            {
                "cadastro_id": "000.003",
                "id_local": "01-004811-001",
                "nro_local": "00002",
                "complemento": "unid.002",
                "area1": "40,60",
            },
            2024,
        )
        self.assertEqual(first["id_local"], second["id_local"])
        self.assertNotEqual(first["cadastro_id_normalizado"], second["cadastro_id_normalizado"])
        self.assertNotEqual(
            MogiPublicExtractor.record_id(first, 1),
            MogiPublicExtractor.record_id(second, 2),
        )
        self.assertEqual(first["area_construcao_m2"], 24.79)
        self.assertEqual(second["area_construcao_m2"], 40.6)

    def test_point_in_polygon_and_multipolygon(self):
        """Valida o cruzamento espacial, inclusive buraco interno e multipolígono."""
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]],
                [[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]],
            ],
        }
        multipolygon = {
            "type": "MultiPolygon",
            "coordinates": [[[[10, 10], [12, 10], [12, 12], [10, 12], [10, 10]]]],
        }
        self.assertTrue(point_in_geometry((1.5, 1.5), polygon))
        self.assertFalse(point_in_geometry((0, 0), polygon))
        self.assertTrue(point_in_geometry((11, 11), multipolygon))
        self.assertFalse(point_in_geometry((20, 20), multipolygon))

    def test_normalizes_common_logradouro_titles(self):
        """Aproxima abreviações equivalentes sem alterar o nome original exportado."""
        self.assertEqual(
            logradouro_key("R TTE GALDINO PINHEIRO"),
            logradouro_key("R TEN. GALDINO PINHEIRO - 08740040"),
        )
        self.assertEqual(
            logradouro_key("AV FREI ATANASIO MAATMAN"),
            logradouro_key("AV FR ATANASIO MAATMAN - 08840215"),
        )

    def test_itbi_does_not_replace_iptu_from_same_year(self):
        """Mantém o IPTU como fonte principal quando IPTU e ITBI têm o mesmo exercício."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                iptu = MogiIptuVenalExtractor.clean_row(
                    {
                        "cadastro_id": "000001",
                        "id_local": "01-000801-001",
                        "exercicio": "2024",
                        "vl_venal_terreno": "100000.00",
                        "vl_venal_construcao": "200000.00",
                    },
                    2024,
                    "pmmc_iptu_2024_part1.csv",
                )
                itbi = MogiItbiVenalExtractor.clean_row(
                    {
                        "cadastro_id": "000.001",
                        "local": "01-000801-001",
                        "exercicio": "2024",
                        "valor_venal": "450.000,00",
                    },
                    2024,
                    "iptu_itbi_2024.csv",
                )
                store.upsert_mogi_iptu_venal([iptu])
                stored = store.upsert_mogi_iptu_venal([itbi], preserve_existing=True)
                self.assertEqual(stored, 0)
                saved = next(store.iter_mogi_iptu_venal())
                self.assertEqual(saved["tipo_fonte"], "iptu")
                self.assertEqual(saved["valor_venal_total"], 300000.0)
            finally:
                store.close()

    def test_exports_each_city_to_its_own_folder(self):
        """Garante que Mogi e Itanhaém não sejam misturados nas saídas individuais."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                store.upsert_records(
                    "teste_cidades",
                    [
                        {
                            "record_id": "mogi-1",
                            "properties": {"cidade": "Mogi das Cruzes", "inscricao_imobiliaria": "01-1"},
                            "geometry": None,
                        },
                        {
                            "record_id": "itanhaem-1",
                            "properties": {"cidade": "Itanhaém", "gid": "10"},
                            "geometry": None,
                        },
                    ],
                )
                counts = export_city_results(store, root / "resultados")
                self.assertEqual(counts["mogi"]["csv"], 1)
                self.assertEqual(counts["itanhaem"]["csv"], 1)
                self.assertEqual(counts["consolidado"]["csv"], 2)
                mogi_csv = (root / "resultados" / "mogi" / "dados_territoriais.csv").read_text("utf-8")
                self.assertIn("Mogi das Cruzes", mogi_csv)
                self.assertNotIn("Itanhaém", mogi_csv)
            finally:
                store.close()

    def test_links_public_iptu_venal_without_payment_fields(self):
        """Garante que a base IPTU forneça apenas os valores venais necessários."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                store.upsert_records(
                    MogiPublicExtractor.source,
                    [{
                        "record_id": "mogi-iptu-1",
                        "properties": {
                            "cidade": "Mogi das Cruzes",
                            "cadastro_id": "000.001",
                            "inscricao_imobiliaria": "01-000801-001",
                        },
                        "geometry": None,
                    }],
                )
                row = MogiIptuVenalExtractor.clean_row(
                    {
                        "cadastro_id": "000001",
                        "id_local": "01-000801-001",
                        "exercicio": "2024",
                        "vl_venal_terreno": "0000227860.91",
                        "vl_venal_construcao": "0000251092.14",
                        "vl_iptu": "00005350.70",
                        "total_lancado": "000005350.70",
                    },
                    2024,
                    "pmmc_iptu_2024_part1.csv",
                )
                self.assertIsNotNone(row)
                store.upsert_mogi_iptu_venal([row])
                linked = apply_mogi_iptu_venal(store)
                self.assertEqual(linked["cadastros_vinculados"], 1)
                record = next(store.iter_records(MogiPublicExtractor.source))["properties_json"]
                self.assertIn('"valor_venal":478953.05', record)
                self.assertNotIn("vl_iptu", record)
                self.assertNotIn("total_lancado", record)
            finally:
                store.close()

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

    def test_imports_fiscal_status_without_boleto_data_and_links_mogi(self):
        """Garante o vínculo do status fiscal agregado sem armazenar boleto."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "situacao_fiscal.csv"
            source.write_text(
                "inscricao_imobiliaria;competencia;possui_pendencia;status_pendencia;valor_total_pendencia;fonte_consulta;data_consulta\n"
                "01-000451-029;2026;sim;em aberto;1.234,56;certidao municipal;2026-08-20\n",
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
                result = import_fiscal_status(store, source, "mogi")
                self.assertEqual(result["importadas"], 1)
                linked = apply_fiscal_status(store)
                self.assertEqual(linked["cadastros_vinculados"], 1)
                record = next(store.iter_records(MogiPublicExtractor.source))
                self.assertIn('"pendencia_fiscal_possui":true', record["properties_json"])
                self.assertIn('"pendencia_fiscal_status":"em aberto"', record["properties_json"])
                self.assertNotIn("boleto", record["properties_json"])
            finally:
                store.close()

    def test_rejects_fiscal_status_with_boleto_or_personal_columns(self):
        """Garante que status fiscal não vire importação de boleto ou CPF."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "situacao_com_boleto.csv"
            source.write_text(
                "inscricao;possui_pendencia;cpf;linha_digitavel\n"
                "01-000451-029;sim;00000000000;00190000090123456789012345678901112345678901234\n",
                encoding="utf-8",
            )
            store = CheckpointStore(root / "checkpoint.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "campos pessoais ou de boleto"):
                    import_fiscal_status(store, source, "mogi")
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
