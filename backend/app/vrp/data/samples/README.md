# Built-in route-plan samples

The web app's Route Plan page offers these instances before any benchmark
instance files are present. They are real Solomon (1987) CVRPTW instances, not
generated data:

| File | Instance | Customers | Fleet |
| --- | --- | --- | --- |
| `C101_25.txt` | Solomon C101, 25-customer version | 25 | 25 vehicles x 200 |
| `R101_25.txt` | Solomon R101, 25-customer version | 25 | 25 vehicles x 200 |

Solomon's 25-customer instances are, by the benchmark's own definition, the depot
plus the first 25 customers of the 100-customer file. Each file here is the
original 100-customer file with every customer row after 25 removed; the header
and all kept rows are unchanged (trailing whitespace and CRLF line endings
stripped).

Source: the SINTEF TOP "Solomon benchmark" page,
`https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip`
(SHA-256 of the archive used:
`8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747`).

Citation: Solomon, M. M. (1987). Algorithms for the Vehicle Routing and
Scheduling Problems with Time Window Constraints. Operations Research, 35(2),
254-265.

Loaded by `app/vrp/instances.py`.
