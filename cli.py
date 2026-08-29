import socket
import sys

def main():
    host = "127.0.0.1"
    port = 8080

    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect((host,port))
        print("Connected to kiln server.")
        print("Commands: BEGIN | GET <key> | PUT <key> <value> | DEL <key> | COMMIT | ABORT | QUIT")
    except Exception as e:
        print(f"Could not connect to server at {host}:{port} - {e}")

        print("Did you start server.py first")

        return

    try:
         while True:
            line = input("> ").strip()
            if not line:
                continue
            if line.upper() in ("QUIT", "EXIT"):
                break

            #send command to the server
            client.sendall((line+ "\n").encode('utf-8'))

            #read response
            response = client.rev(4096).decode('utf-8').strip()
            if not response:
                print("connection closed by server")

            print(response)


    except KeyboardInterrupt:
           print("\n Exiting...")

    except Exception as e:
        print(f"error: {e}")

    finally:
        client.close()


if __name__ == "__main__":
    main()

