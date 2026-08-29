import socket
import threading
from engine import Engine, ConflictError
from txn import Transaction

class kilnServer:
    def __init__(self, data_dir:str, host: str = "127.0.0.1", port:int = 8080):
        self.db= Engine(data_dir)
        self.host = host
        self.port = port

        #Setup TCP Socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def start(self):
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen()
        print(f"KILN server listening on {self.host}:{self.port}")

        try:
            while True:
                client_socket, addr = self.server_socket.accept()
                print(f"Accepted connection from {addr}")
                #Handle each client in a separate thread
                client_thread = threading.Thread(
                    target = self.handle_client,
                    args=(client_socket,),
                    daemon=True
                )
                client_thread.start()

        except KeyboardInterrupt:
            print("\nShutting down server...")

        finally:
            self.server_socket.close()
            self.db.close()


    def handle_client(self, client_socket: socket.socket):
        #1 connection = 1 active transaction max
        active_txn: Transaction= None

        def send(msg: str):
            client_socket.sendall((msg+"\n").encode('utf-8'))

        try:
            buffer = ""
            while True:
                data = client_socket.recv(4096)
                if not data:
                    break

                buffer+= data.decode('utf-8')


                #process line by line

                while '\n' in buffer:
                    line, buffer = buffer.split('\n',1)
                    line= line.strip()
                    if not line:
                        continue

                    parts = line.split(" ")
                    cmd = parts[0].upper()

                    try:
                        if cmd == "BEGIN":
                            if active_txn is not None:
                                send("ERR already_in_txn")
                            else:
                                active_txn = self.db.begin()
                                send(f"OK t={active_txn.txn_id} start_ts={active_txn.starts_ts}")


                        elif cmd== "GET":
                            if active_txn is None:
                                send("ERR not_in_txn")
                            elif len(parts) != 2:
                                send("ERR usage: GET <key>")
                            else:
                                key = parts[1].encode('utf-8')
                                val = self.db.get(active_txn, key)
                                if val is None:
                                    send("NOTFOUND")

                                else:
                                    send(f"VALUE {val.decode('utf-8')}")
                        
                        elif cmd == "PUT":
                            if active_txn is None:
                                send("ERR not_in_txn")

                            elif len(parts) < 3:
                                send("ERR usage: PUT <key> <value>")


                            else:
                                key= parts[1].encode('utf-8')
                                #JOIN remaining parts to allow spaces in value
                                val = "".join(parts[2:]).encode('utf-8')
                                self.db.put(active_txn, key, val)
                                send("OK")

                        elif cmd == "DEL":
                            if active_txn is None:
                                send("ERR not_in_txn")
                            elif len(parts) !=2:
                                send("ERR uage: DEL <key>")
                            else:
                                key = parts[1].encode('utf-8')
                                self.db.delete(active_txn, key)
                                send("OK")

                        elif cmd == "COMMIT":
                            if active_txn is None:
                                send("ERR not_in_txn")

                            else:
                                try:
                                    self.db.commit(active_txn)
                                    send("OK")

                                except ConflictError:
                                    send("ERR conflict")

                                active_txn = None
                        
                        elif cmd == "ABORT":
                            if active_txn is None:
                                send("ERR not_in_txn")

                            else:
                                self.db.abort(active_txn)
                                send("OK")
                                active_txn=None

                        else:
                            send(f"ERR unknown_command {cmd}")


                    except Exception as e:
                      send(f"ERR server_error {str(e)}")
                      if active_txn:
                        self.db.abort(active_txn)
                        active_txn = None

        except Exception as e:
            print(f"Client error: {e}")
        finally:
            if active_txn is not None:
                try:
                    self.db.abort(active_txn)

                except:
                    pass

            client_socket.close()

if __name__ == "__main__":
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "kiln-data"
    server = kilnServer(data_dir)
    server.start()
