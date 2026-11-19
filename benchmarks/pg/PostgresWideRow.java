/*
 * Copyright © 2021-present Arcade Data Ltd (info@arcadedata.com)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * SPDX-FileCopyrightText: 2021-present Arcade Data Ltd (info@arcadedata.com)
 * SPDX-License-Identifier: Apache-2.0
 */

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/**
 * Splits the cost of a wide row over the Postgres wire: how much of it is the row's columns travelling
 * and how much is the record behind them being read.
 * <ul>
 * <li>{@code SELECT * FROM W} — every column, projected one by one;</li>
 * <li>{@code SELECT (@this) FROM W} — the record as ONE value, so the projection does not walk it
 * property by property;</li>
 * <li>{@code SELECT f0 FROM W} — one column.</li>
 * </ul>
 * Usage: {@code java PostgresWideRow <port> <password> <rows> <P...>}
 */
public class PostgresWideRow {

  public static void main(final String[] args) throws Exception {
    final int port = Integer.parseInt(args[0]);
    final String password = args[1];
    final int rows = Integer.parseInt(args[2]);

    System.out.printf("%6s %14s %16s %14s%n", "P", "SELECT *", "one value", "one column");
    for (int p = 3; p < args.length; p++) {
      final int properties = Integer.parseInt(args[p]);
      final String db = "pgwide" + properties;

      final Properties props = new Properties();
      props.setProperty("user", "root");
      props.setProperty("password", password);
      props.setProperty("sslmode", "disable");
      try (final Connection conn =
               DriverManager.getConnection("jdbc:postgresql://127.0.0.1:" + port + "/" + db, props)) {
        final double whole = measure(conn, "SELECT * FROM W", rows);
        final double inside = measure(conn, "SELECT (@this) FROM W", rows);
        final double one = measure(conn, "SELECT f0 FROM W", rows);
        System.out.printf(
            "%6d %10.1f us %13.1f us %10.1f us%n", properties, whole, inside, one);
      }
    }
  }

  private static double measure(final Connection conn, final String query, final int expectedRows) throws Exception {
    final List<Double> samples = new ArrayList<>();
    for (int run = 0; run < 7; run++) {
      final long start = System.nanoTime();
      int count = 0;
      try (final Statement st = conn.createStatement(); final ResultSet rs = st.executeQuery(query)) {
        while (rs.next())
          count++;
      }
      if (count != expectedRows)
        throw new IllegalStateException(query + " answered " + count + " rows, expected " + expectedRows);
      samples.add((System.nanoTime() - start) / 1e3 / expectedRows);
    }
    // the median of the last five runs
    final List<Double> measured = samples.subList(2, samples.size());
    measured.sort(null);
    return measured.get(measured.size() / 2);
  }
}
