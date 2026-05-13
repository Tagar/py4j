# py4j perf report

**Branch:** perf-framework-v2 (rev f9ced1a, dirty)  
**Timestamp:** 2026-05-13T04:33:01+00:00  
**OS / CPU:** Darwin 25.4.0 (arm64) - Apple M4 Pro @ 0.00 GHz, 14 physical / 14 logical cores  
**RAM:** 48.0 GB  
**Python / Java:** 3.14.3 (CPython) / openjdk version "25.0.2" 2026-01-20  
**py4j:** 0.10.9.9
**Process priority:** nice=0 (renice not applied: --no-renice)

| ID | Scenario | Median | Latency/op | Throughput | Bandwidth | CPU/wall | p95 | Stddev | Noise | Rounds | CV vs budget | Errors |
|----|----------|--------|------------|------------|-----------|----------|-----|--------|-------|--------|--------------|--------|
| M1 | m1_static_call_no_args | 22.383 µs | 23.371 ns | 42.79 M ops/s | n/a | n/a | 26.490 µs | 1.752 µs | 23.3% | 150 | n/a | 0 |
| M2a | m2a_instance_append_int | 43.769 µs | 44.156 ns | 22.65 M ops/s | n/a | n/a | 47.889 µs | 1.655 µs | 12.2% | 150 | n/a | 0 |
| M2b | m2b_instance_append_str | 44.533 µs | 45.189 ns | 22.13 M ops/s | n/a | n/a | 47.714 µs | 1.796 µs | 10.3% | 150 | n/a | 0 |
| M3 | m3_jvmview_class_resolution | 84.348 µs | 170.775 ns | 5.86 M ops/s | n/a | n/a | 88.946 µs | 2.423 µs | 6.7% | 150 | n/a | 0 |
| M4 | m4_constructor_and_finalize | 128.310 µs | 261.003 ns | 3.83 M ops/s | n/a | n/a | 139.062 µs | 4.772 µs | 10.3% | 150 | n/a | 0 |
| M5a | m5a_encode_int | 215.420 ns | 2.150 ns | 465.12 M ops/s | n/a | n/a | 225.829 ns | 10.467 ns | 13.0% | 1415505 | n/a | 0 |
| M5b | m5b_encode_string | 377.799 ns | 26.786 ns | 37.33 M ops/s | n/a | n/a | 392.852 ns | 28.545 ns | 12.6% | 5844214 | n/a | 0 |
| M5c | m5c_encode_float | 343.745 ns | 21.324 ns | 46.90 M ops/s | n/a | n/a | 351.567 ns | 24.522 ns | 9.1% | 5918714 | n/a | 0 |
| M6a | m6a_decode_int | 135.411 ns | 1.363 ns | 733.94 M ops/s | n/a | n/a | 139.581 ns | 6.787 ns | 8.9% | 2315107 | n/a | 0 |
| M6b | m6b_decode_string | 429.456 ns | 32.545 ns | 30.73 M ops/s | n/a | n/a | 444.425 ns | 33.664 ns | 7.2% | 5933963 | n/a | 0 |
| M7a | m7a_escape | 904.198 ns | 85.001 ns | 11.76 M ops/s | n/a | n/a | 975.002 ns | 966.870 ns | 18.4% | 3553345 | n/a | 0 |
| M7b | m7b_unescape | 3.333 µs | 1.667 µs | 600.05 k ops/s | n/a | n/a | 3.500 µs | 430.500 ns | 14.4% | 4700370 | n/a | 0 |
| X1-1 | concurrent_1_thread | 226.305 ms | 22.612 µs | 44.22 k ops/s | n/a | 0.44 | 231.969 ms | 5.186 ms | 4.7% | 150 | 0.10x | 0 |
| X1-4 | concurrent_4_threads | 129.395 ms | 12.838 µs | 77.90 k ops/s | n/a | 1.86 | 132.152 ms | 1.976 ms | 3.7% | 150 | 0.09x | 0 |
| X1-16 | concurrent_16_threads | 143.627 ms | 14.311 µs | 69.88 k ops/s | n/a | 2.15 | 147.176 ms | 2.553 ms | 5.2% | 150 | 0.20x | 0 |
| X2-1k | iterate_javalist_1k | 208.084 ms | 20.770 µs | 48.15 k ops/s | 2.30 MB/s | 0.48 | 214.297 ms | 2.650 ms | 4.1% | 150 | 0.10x | 0 |
| X2-10k | iterate_javalist_10k | 208.602 ms | 20.598 µs | 48.55 k ops/s | 2.31 MB/s | 0.49 | 215.977 ms | 3.394 ms | 5.5% | 150 | 0.10x | 0 |
| X2-100k | iterate_javalist_100k | 2.056 s | 20.625 µs | 48.48 k ops/s | 2.31 MB/s | 0.49 | 2.149 s | 30.939 ms | 5.5% | 89 (budget) | 0.12x | 0 |
| X3-100 | listconverter_100 | 58.615 ms | 25.100 µs | 39.84 k ops/s | 1.71 MB/s | 0.42 | 65.576 ms | 6.218 ms | 30.3% | 150 | 0.39x | 0 |
| X3-1k | listconverter_1k | 227.123 ms | 22.693 µs | 44.07 k ops/s | 1.89 MB/s | 0.45 | 232.579 ms | 3.625 ms | 4.8% | 150 | 0.16x | 0 |
| X3-10k | listconverter_10k | 228.153 ms | 22.728 µs | 44.00 k ops/s | 1.89 MB/s | 0.46 | 232.915 ms | 4.255 ms | 4.8% | 150 | 0.22x | 0 |
| X4 | callback_sort_100_items | 94.875 ms | 94.909 µs | 10.54 k ops/s | n/a | 0.48 | 112.899 ms | 7.102 ms | 20.2% | 150 | 0.74x | 0 |
| X5 | error_path_latency | 60.607 ms | 52.600 µs | 19.01 k ops/s | n/a | 0.43 | 67.002 ms | 5.887 ms | 30.0% | 150 | 0.49x | 0 |
| X6 | pool_saturation_50_threads | 82.796 ms | 16.584 µs | 60.30 k ops/s | n/a | 2.09 | 84.321 ms | 1.329 ms | 3.9% | 150 | 0.21x | 0 |

*Noise = (p95 - p5) / median within a single run.*
*CV vs budget = observed coefficient of variation divided by the scenario's declared `expected_cv`. Values >= 2x are flagged with `!!` — either the scenario is unstable or the runner environment is too noisy for the verdict to be trusted.*
