import sqlite3
import threading
import unittest

from servidor_conferencia_mogi import MogiDataRepository


class MogiDataRepositoryTest(unittest.TestCase):
    """Protege as regras de busca e agregação usadas pela interface local."""

    def setUp(self) -> None:
        """Cria um índice mínimo em memória sem depender dos arquivos locais de 233 mil linhas."""
        self.repository = object.__new__(MogiDataRepository)
        self.repository.lock = threading.RLock()
        self.repository.index = sqlite3.connect(":memory:")
        self.repository.index.row_factory = sqlite3.Row
        self.repository.index.execute(
            """
            CREATE TABLE cadastros (
                cadastro_id_normalizado TEXT PRIMARY KEY,
                cadastro_id TEXT NOT NULL,
                id_local_normalizado TEXT,
                id_local TEXT,
                logradouro TEXT,
                numero TEXT,
                complemento TEXT,
                tipo TEXT,
                uso_imovel TEXT,
                bairro TEXT,
                zona TEXT,
                situacao_valor TEXT,
                precisao TEXT,
                contexto_status TEXT,
                valor_venal REAL,
                exercicio_valor INTEGER,
                longitude REAL,
                latitude REAL,
                search_text TEXT NOT NULL
            )
            """
        )
        rows = [
            (
                "000001", "000.001", "01004811001", "01-004811-001",
                "R DR CORREA", "2", "unid.001", "predial", "residencia",
                "CENTRO", "ZC-A", "disponivel_exercicio_principal",
                "centro_da_quadra", "cruzado_com_camadas_publicas", 100000.0,
                2024, -46.19, -23.52, "000 001 01 004811 001 r dr correa centro zc a",
            ),
            (
                "000002", "000.002", "01004811001", "01-004811-001",
                "R DR CORREA", "2", "unid.002", "predial", "residencia",
                "CENTRO", "ZC-A", "disponivel_exercicio_principal",
                "centro_da_quadra", "cruzado_com_camadas_publicas", 120000.0,
                2024, -46.19, -23.52, "000 002 01 004811 001 r dr correa centro zc a",
            ),
            (
                "000003", "000.003", "01000801001", "01-000801-001",
                "R ANTONIO VIEIRA", "1", "unica", "predial", "comercio",
                "CENTRO", "ZC-A", "disponivel_historico", "centro_do_logradouro",
                "cruzado_com_camadas_publicas", 90000.0, 2023, -46.18, -23.51,
                "000 003 01 000801 001 r antonio vieira centro zc a",
            ),
        ]
        self.repository.index.executemany(
            "INSERT INTO cadastros VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def tearDown(self) -> None:
        """Fecha o banco temporário depois de cada regra verificada."""
        self.repository.index.close()

    def test_map_groups_all_filtered_records_by_coordinate(self) -> None:
        """O mapa agrega coordenadas iguais sem limitar os pontos à página da lista."""
        payload = self.repository.map_points({})

        self.assertEqual(payload["total_pontos"], 2)
        self.assertEqual(payload["cadastros_georreferenciados"], 3)
        self.assertEqual(payload["pontos"][0]["cadastros"], 2)
        self.assertEqual(payload["pontos"][0]["ids_locais"], 1)

    def test_exact_id_local_keeps_individual_cadastros_in_the_list(self) -> None:
        """Pesquisar um id_local compartilhado devolve todas as linhas cadastrais ligadas a ele."""
        payload = self.repository.search({"q": ["01-004811-001"], "limite": ["30"]})

        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            [item["cadastro_id"] for item in payload["resultados"]],
            ["000.001", "000.002"],
        )


if __name__ == "__main__":
    unittest.main()
