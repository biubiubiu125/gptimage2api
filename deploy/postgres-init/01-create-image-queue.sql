SELECT 'CREATE DATABASE gptimage2api_image_queue'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'gptimage2api_image_queue'
)\gexec
