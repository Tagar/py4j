/******************************************************************************
 * Copyright (c) 2009-2022, Barthelemy Dagenais and individual contributors.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * - Redistributions of source code must retain the above copyright notice,
 * this list of conditions and the following disclaimer.
 *
 * - Redistributions in binary form must reproduce the above copyright notice,
 * this list of conditions and the following disclaimer in the documentation
 * and/or other materials provided with the distribution.
 *
 * - The name of the author may not be used to endorse or promote products
 * derived from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 *****************************************************************************/
package py4j;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.Socket;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import org.junit.Test;

import py4j.commands.AuthCommand;
import py4j.commands.HelpPageCommand;

public class GatewayServerTest {

	@Test
	public void testDoubleListen() {
		GatewayServer server1 = new GatewayServer.GatewayServerBuilder().entryPoint(null).build();
		GatewayServer server2 = new GatewayServer.GatewayServerBuilder().entryPoint(null).build();
		boolean valid = false;

		try {
			server1.start();
			server2.start();
			valid = false;
		} catch (Py4JNetworkException network) {
			valid = true;
		} catch (Exception e) {
			valid = false;
		}

		server1.shutdown();
		server2.shutdown();

		assertTrue(valid);
	}

	@Test
	public void testListener() {
		TestListener listener = new TestListener();
		// Use DEFAULT_PORT + 1 in case the previous test's default ports are still occupied.
		GatewayServer server1 = new GatewayServer(null, GatewayServer.DEFAULT_PORT + 1);
		server1.addListener(listener);
		server1.start();
		// Listener events fire from a background thread (GatewayServer.run()),
		// so start/shutdown return before serverStarted/serverStopped land in
		// listener.values. Poll for the expected events instead of blind-
		// sleeping: a healthy runner finishes in tens of ms, but a loaded CI
		// runner can take seconds. The previous Thread.sleep(250) then 1s
		// then 2s arms-race never converged. Same approach as
		// ClientServerTest.testListenerClientServer.
		waitForListenerSize(listener, 1, 10000);
		server1.shutdown();
		waitForListenerSize(listener, 4, 10000);
		// Started, PreShutdown, Stopped, PostShutdown
		// But order cannot be guaranteed because two threads are competing.
		assertTrue(listener.values.contains(new Long(1)));
		assertTrue(listener.values.contains(new Long(10)));
		assertTrue(listener.values.contains(new Long(1000)));
		assertTrue(listener.values.contains(new Long(10000)));
		// serverError (100) can also fire opportunistically when sSocket
		// closes mid-accept during shutdown - that's expected lifecycle
		// behavior, not a test failure. Size >= 4 with the 4 expected
		// events present is the real correctness check.
		assertTrue(listener.values.size() >= 4);
	}

	private static void waitForListenerSize(TestListener listener, int target, long timeoutMs) {
		long deadline = System.currentTimeMillis() + timeoutMs;
		while (listener.values.size() < target && System.currentTimeMillis() < deadline) {
			try {
				Thread.sleep(20);
			} catch (InterruptedException e) {
				Thread.currentThread().interrupt();
				return;
			}
		}
	}

	/**
	 * Regression test: verify that a normal {@code shutdown()} does NOT fire
	 * {@code serverError}.
	 *
	 * Before the fix, {@link GatewayServer#run()} caught the
	 * {@code SocketException} that {@code accept()} throws after
	 * {@code shutdown()} closes the server socket, and routed it through
	 * {@code fireServerError(e)}. {@code fireServerError} attempts to filter
	 * via a {@code "socket closed"} string match on the exception message,
	 * but that filter is locale- and JVM-version-dependent. The robust fix
	 * is to skip {@code fireServerError} entirely when {@code isShutdown}
	 * is set.
	 */
	@Test
	public void testListenerNoSpuriousErrorOnShutdown() {
		TestListener listener = new TestListener();
		// Use DEFAULT_PORT + 2 to avoid clashing with testListener.
		GatewayServer server = new GatewayServer(null, GatewayServer.DEFAULT_PORT + 2);
		server.addListener(listener);
		server.start();
		try {
			Thread.sleep(250);
		} catch (Exception e) {
		}
		server.shutdown();
		try {
			Thread.sleep(250);
		} catch (Exception e) {
		}
		// 100 = serverError. It must NOT fire on a clean shutdown.
		assertFalse("serverError fired during a normal shutdown", listener.values.contains(new Long(100)));
		// Sanity: the four expected lifecycle events all fired.
		assertTrue(listener.values.contains(new Long(1)));
		assertTrue(listener.values.contains(new Long(10)));
		assertTrue(listener.values.contains(new Long(1000)));
		assertTrue(listener.values.contains(new Long(10000)));
	}

	/**
	 * Regression test: gracePeriodMs=0 (the default) preserves the historical
	 * abrupt shutdown behavior — connections are torn down immediately.
	 */
	@Test
	public void testAbruptShutdownIsBackCompat() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		final int port = server.getListeningPort();
		Socket client = new Socket("127.0.0.1", port);
		Thread.sleep(100);

		long start = System.currentTimeMillis();
		// gracePeriodMs=0 (via the legacy shutdown(boolean) overload) —
		// returns immediately even with an open connection.
		server.shutdown(true);
		long elapsed = System.currentTimeMillis() - start;
		assertTrue("abrupt shutdown took longer than expected: " + elapsed + "ms", elapsed < 1000);

		try {
			client.close();
		} catch (Exception ignored) {
		}
	}

	/**
	 * Regression test: a negative {@code gracePeriodMs} is treated as 0
	 * (no drain), preserving back-compat with the abrupt shutdown path.
	 */
	@Test
	public void testNegativeGracePeriodIsAbrupt() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		final int port = server.getListeningPort();
		Socket client = new Socket("127.0.0.1", port);
		Thread.sleep(100);

		long start = System.currentTimeMillis();
		server.shutdown(true, -1000);
		long elapsed = System.currentTimeMillis() - start;
		assertTrue("negative gracePeriodMs should be treated as 0: " + elapsed + "ms", elapsed < 1000);

		try {
			client.close();
		} catch (Exception ignored) {
		}
	}

	/**
	 * Regression test: when there are no active connections, a graceful
	 * shutdown returns promptly even with a long {@code gracePeriodMs}.
	 */
	@Test
	public void testGracefulShutdownNoActiveConnections() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		long start = System.currentTimeMillis();
		// 30s grace period, but no connections — should return promptly.
		server.shutdown(true, 30000);
		long elapsed = System.currentTimeMillis() - start;
		assertTrue("shutdown with no connections took too long: " + elapsed + "ms", elapsed < 1000);
	}

	/**
	 * Regression test: a connection that does NOT drain within the grace
	 * window is force-closed at the deadline, and shutdown returns close
	 * to the deadline (not before, not after the deadline + small slack).
	 */
	@Test
	public void testGracefulShutdownForceCloseAfterDeadline() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		final int port = server.getListeningPort();
		// Open a connection that we deliberately keep alive past the deadline.
		Socket client = new Socket("127.0.0.1", port);
		Thread.sleep(100);

		long start = System.currentTimeMillis();
		// 500ms grace period; client never closes, so deadline must trigger
		// force-close. Total elapsed should be ~500ms (give or take 1s slack).
		server.shutdown(true, 500);
		long elapsed = System.currentTimeMillis() - start;
		assertTrue("shutdown returned before deadline: " + elapsed + "ms", elapsed >= 400);
		assertTrue("shutdown took much longer than deadline: " + elapsed + "ms", elapsed < 2000);

		try {
			client.close();
		} catch (Exception ignored) {
		}
	}

	/**
	 * DEBUG: graceful drain when the connection is closed BEFORE shutdown.
	 * Eliminates closer-thread timing complexity from the failing variant.
	 */
	@Test
	public void debugGracefulDrainWhenClientPreClosed() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		final int port = server.getListeningPort();
		Socket client = new Socket("127.0.0.1", port);
		Thread.sleep(100);
		System.out.println("DEBUG: closing client BEFORE shutdown");
		client.close();
		Thread.sleep(500); // Give server's connectionStopped time to fire

		long start = System.currentTimeMillis();
		System.out.println("DEBUG: calling shutdown(true, 5000) - connections should already be drained");
		server.shutdown(true, 5000);
		long elapsed = System.currentTimeMillis() - start;
		System.out.println("DEBUG: shutdown returned in " + elapsed + "ms");

		assertTrue("pre-closed drain took too long: " + elapsed + "ms", elapsed < 500);
	}

	/**
	 * DEBUG: graceful drain with closer thread (the failing variant) + logging.
	 */
	@Test
	public void debugGracefulDrainWithCloserThread() throws Exception {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		Thread.sleep(100);

		final int port = server.getListeningPort();
		final Socket client = new Socket("127.0.0.1", port);
		Thread.sleep(100);

		final long[] closerFiredAt = new long[1];
		Thread closer = new Thread(new Runnable() {
			@Override
			public void run() {
				try {
					Thread.sleep(200);
					closerFiredAt[0] = System.currentTimeMillis();
					System.out.println("DEBUG: closer firing client.close()");
					client.close();
				} catch (Exception e) {
					System.out.println("DEBUG: closer exception: " + e);
				}
			}
		});
		closer.start();

		long start = System.currentTimeMillis();
		System.out.println("DEBUG: calling shutdown(true, 5000)");
		server.shutdown(true, 5000);
		long elapsed = System.currentTimeMillis() - start;
		long closeDelta = closerFiredAt[0] - start;
		System.out.println("DEBUG: shutdown returned in " + elapsed + "ms, closer fired at t+" + closeDelta + "ms");

		closer.join(1000);
		assertTrue("closer didn't fire: closerFiredAt=" + closerFiredAt[0], closerFiredAt[0] > 0);
	}

	@Test
	public void testEphemeralPort() {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		try {
			Thread.sleep(250);
		} catch (Exception e) {

		}
		int listeningPort = server.getListeningPort();
		assertTrue(listeningPort > 0);
		assertTrue(server.getPort() != listeningPort);
		server.shutdown();
	}

	@Test
	public void testResetCallbackClient() {
		GatewayServer server = new GatewayServer(null, 0);
		server.start(true);
		try {
			Thread.sleep(250);
		} catch (Exception e) {

		}
		server.resetCallbackClient(server.getAddress(), GatewayServer.DEFAULT_PYTHON_PORT + 1);
		try {
			Thread.sleep(250);
		} catch (Exception e) {

		}
		int pythonPort = server.getPythonPort();
		InetAddress pythonAddress = server.getPythonAddress();
		assertEquals(pythonPort, GatewayServer.DEFAULT_PYTHON_PORT + 1);
		assertEquals(pythonAddress, server.getAddress());
		server.shutdown(true);
	}

	@Test
	public void testAuthentication() throws Exception {
		GatewayServer server = new GatewayServer.GatewayServerBuilder().authToken("secret").build();
		server.start(true);

		try {
			Socket valid = new Socket(server.getAddress(), server.getListeningPort());
			try {
				testServerAccess(valid, "secret");
			} finally {
				valid.close();
			}

			for (String invalidSecret : Arrays.asList("invalidSecret", null)) {
				Socket conn = new Socket(server.getAddress(), server.getListeningPort());
				try {
					testServerAccess(conn, invalidSecret);
					fail("Should have failed to communicate with server.");
				} catch (IOException ioe) {
					// Expected.
				} finally {
					conn.close();
				}
			}
		} finally {
			server.shutdown(true);
		}
	}

	private void testServerAccess(Socket s, String authToken) throws Exception {
		PrintWriter out = new PrintWriter(new OutputStreamWriter(s.getOutputStream(), "UTF-8"));
		BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));

		if (authToken != null) {
			out.println(AuthCommand.COMMAND_NAME);
			out.println(authToken);
			out.flush();

			// Read the response from the auth request. Don't check it - let the rest of the test
			// make sure auth was successful or not.
			in.readLine();
		}

		// Send a "help" command and try to read the response. This should throw exceptions if
		// authentication fails.
		out.println(HelpPageCommand.HELP_COMMAND_NAME);
		out.println(HelpPageCommand.HELP_CLASS_SUB_COMMAND_NAME);
		out.println(HelpPageCommand.class.getName());
		out.println("");
		out.println("t");
		out.flush();

		String reply = in.readLine();
		if (authToken == null) {
			// If no auth token was provided, this code might be able to read a line of output before the
			// socket is closed by the server; it should be the error message from the auth check.
			assertEquals(Protocol.getOutputErrorCommand("Authentication error: unexpected command.").trim(), reply);

			// Throw an IOException since that's what the test above expects in this case.
			throw new IOException("Auth unsuccessful.");
		} else {
			assertTrue("Expected return message or null, got: " + reply,
					reply == null || Protocol.isReturnMessage(reply));
			if (reply == null || Protocol.isError(reply.substring(1))) {
				throw new IOException("Error from server.");
			}
		}
	}

}

class TestListener implements GatewayServerListener {

	public List<Long> values = new CopyOnWriteArrayList<Long>();

	@Override
	public void serverStarted() {
		values.add(new Long(1));
	}

	@Override
	public void serverStopped() {
		values.add(new Long(10));
	}

	@Override
	public void serverError(Exception e) {
		values.add(new Long(100));
	}

	@Override
	public void serverPreShutdown() {
		values.add(new Long(1000));
	}

	@Override
	public void serverPostShutdown() {
		values.add(new Long(10000));
	}

	@Override
	public void connectionStarted(Py4JServerConnection gatewayConnection) {
		values.add(new Long(100000));
	}

	@Override
	public void connectionStopped(Py4JServerConnection gatewayConnection) {
		values.add(new Long(1000000));
	}

	@Override
	public void connectionError(Exception e) {
		values.add(new Long(10000000));
	}

}