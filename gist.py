import time
import functools


def cache_clean(expiration: int):
    """
    Decorator that clears the cache of a function decorated with functools.lru_cache
    or functools.cache every 'expiration' seconds.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            now = time.perf_counter()
            if not hasattr(wrapper, 'next_clean'):
                wrapper.next_clean = now + expiration

            if now > wrapper.next_clean:
                clear_func = getattr(wrapper, 'cache_clear', None)
                if clear_func:
                    clear_func()
                wrapper.next_clean = now + expiration

            return func(*args, **kwargs)

        wrapper.cache_info = getattr(func, 'cache_info', None)
        wrapper.cache_clear = getattr(func, 'cache_clear', None)
        return wrapper

    return decorator


@cache_clean(expiration=1)
@functools.cache
def test_func(n: int) -> int:
    return n * 2


if __name__ == "__main__":
    print("Testing cache_clean...")
    # First call: cache miss
    test_func(10)
    print(f"Cache info after 1st call: {test_func.cache_info()}")

    # Second call: cache hit
    test_func(10)
    print(f"Cache info after 2nd call (hit): {test_func.cache_info()}")

    print("Waiting for expiration (1.1s)...")
    time.sleep(1.1)

    # Third call: should have triggered cache_clear, so cache miss
    test_func(10)
    print(f"Cache info after expiration (should be miss): {test_func.cache_info()}")

