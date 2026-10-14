import asyncio

from app.agent_events import AgentEventHub


def test_stop_closes_listener_connection_before_waiting_for_task() -> None:
    async def exercise() -> None:
        hub = AgentEventHub()
        connection_closed = asyncio.Event()

        class BlockingConnection:
            closed = False

            async def close(self) -> None:
                self.closed = True
                connection_closed.set()

        async def blocked_listener() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await connection_closed.wait()
                raise

        connection = BlockingConnection()
        task = asyncio.create_task(blocked_listener())
        await asyncio.sleep(0)
        hub._listener_connection = connection
        hub._task = task

        await hub.stop()

        assert connection.closed is True
        assert task.cancelled()

    asyncio.run(exercise())
