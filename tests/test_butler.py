import httpx
import pytest

from euroleague_notifier.butler import ButlerClient, ButlerError


def client(response: httpx.Response) -> ButlerClient:
    transport = httpx.MockTransport(lambda request: response)
    return ButlerClient("http://butler", "k", "p", transport=transport, backoff=0)


async def test_empty_success_body_is_an_empty_result():
    async with client(httpx.Response(204)) as butler:
        await butler.register({"name": "x"})
        assert await butler.notify("k1", "hi") == {}


async def test_invalid_json_success_body_is_a_butler_error():
    async with client(httpx.Response(200, content=b"<html>proxy</html>")) as butler:
        with pytest.raises(ButlerError, match="invalid JSON"):
            await butler.notify("k1", "hi")
