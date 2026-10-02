import unittest

from remote_desktop.gateway import EdgeAddress, EdgeGateway


class DummyWriter:
    def close(self):
        pass

    async def wait_closed(self):
        pass


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_chooses_least_active_edge(self):
        gateway = EdgeGateway(
            [
                EdgeAddress.parse("edge-a=127.0.0.1:8001"),
                EdgeAddress.parse("edge-b=127.0.0.1:8002"),
            ]
        )
        gateway.targets[0].active = 3

        target = await gateway._choose_target(set())

        self.assertEqual(target.address.edge_id, "edge-b")

    async def test_failed_edge_is_skipped_for_next_connection(self):
        attempts = []

        async def connector(host, port):
            attempts.append(port)
            if port == 8001:
                raise OSError("down")
            return object(), DummyWriter()

        gateway = EdgeGateway(
            [
                EdgeAddress.parse("edge-a=127.0.0.1:8001"),
                EdgeAddress.parse("edge-b=127.0.0.1:8002"),
            ],
            connector=connector,
        )

        target, _, _ = await gateway._connect_to_edge()

        self.assertEqual(target.address.edge_id, "edge-b")
        self.assertEqual(attempts, [8001, 8002])
        self.assertGreater(gateway.targets[0].unhealthy_until, 0)

    def test_edge_address_parse(self):
        address = EdgeAddress.parse("edge-a=10.0.0.5:8023")

        self.assertEqual(address.edge_id, "edge-a")
        self.assertEqual(address.host, "10.0.0.5")
        self.assertEqual(address.port, 8023)


if __name__ == "__main__":
    unittest.main()

