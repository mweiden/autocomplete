import logging
import asyncio
import sys
import time

from selenium.common.exceptions import WebDriverException
from urllib3.exceptions import MaxRetryError
from web_crawler.web_scraper import WebScraper
from web_crawler.node import Node

from search.inverted_index import InvertedIndex
from pickle_store import PickleStore
from env import INVERTED_INDEX_STORAGE_PATH

logging.basicConfig(level=logging.INFO, stream=sys.stdout)


START_TIME = time.perf_counter()


def seconds_since_program_start():
    current_time = time.perf_counter()
    return int(current_time - START_TIME)


async def worker(
    worker_id: int,
    inverted_index: InvertedIndex,
    max_depth: int,
    visited: set[str],
    netloc_last_visited_at: dict[str, int],
    queue: asyncio.PriorityQueue,
) -> None:
    logger = logging.getLogger(f"worker{worker_id}")

    scraper = WebScraper()

    logger.info("Started worker.")

    while True:
        try:
            node = await queue.get()

            fetch_time = await scraper.navigate(node.url)
            netloc_last_visited_at[node.netloc] = seconds_since_program_start()

            node.text = scraper.extract_rendered_text()
            node.title = scraper.driver.title

            inverted_index.insert(node)

            links = scraper.find_all_links()

            if (node.depth + 1) <= max_depth:
                for link in links:
                    link_node = Node(link, node.depth + 1)
                    # we prefer net locations we haven't seen before to avoid rate limiting
                    if netloc_last_visited_at.get(link_node.netloc) is not None:
                        # prioritize longer time since last visit; mult by -1 since PriorityQueue prioritizes lower numbers
                        link_node.priority = -1 * (
                            time.perf_counter()
                            - netloc_last_visited_at[link_node.netloc]
                        )
                    if link_node.url in visited:
                        continue
                    visited.add(link_node.url)
                    await queue.put(link_node)

            logger.info(
                f"index_bytes={sys.getsizeof(inverted_index._inverted_index)} "
                f"queue_size={queue.qsize()} "
                f"depth={node.depth} "
                f"priority={node.priority} "
                f"fetch_time={round(fetch_time, 4)}s "
                f"url={node.url}"
            )
            queue.task_done()
            if queue.empty():
                break
        except (WebDriverException, MaxRetryError) as e:
            logger.error(f"Failed to fetch error {e} URL: {node.url}")
            queue.task_done()
            if queue.empty():
                break


async def main():
    max_depth = 3
    num_workers = 32
    seed_url = "https://news.ycombinator.com"

    inverted_index = InvertedIndex()
    pickle_store = PickleStore(f"../{INVERTED_INDEX_STORAGE_PATH}")

    seed_node = Node(seed_url)
    visited = set([seed_node.url])
    netloc_last_visited_at = dict()

    queue = asyncio.PriorityQueue()

    # start with the seed url
    await queue.put(seed_node)

    # start workers
    workers = [
        asyncio.create_task(
            worker(
                worker_id=i,
                inverted_index=inverted_index,
                max_depth=max_depth,
                visited=visited,
                netloc_last_visited_at=netloc_last_visited_at,
                queue=queue,
            )
        )
        for i in range(num_workers)
    ]

    await queue.join()

    for w in workers:
        w.cancel()

    await asyncio.gather(*workers, return_exceptions=True)

    pickle_store.save(inverted_index)


if __name__ == "__main__":
    asyncio.run(main())
