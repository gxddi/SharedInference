"""
Stakes, payouts, releases and slashes: the coordinator's side of the Solana program in sinference/solana/.

Each account's wallet (its private key added on the nodes page or by `sinference up`,
kept in the database; devnet only) backs all its nodes. When a node joins short of
collateral ($20 of SOL per node), the coordinator stakes the difference from that
wallet into the account's Stake account, at ["stake", wallet, account id]. Finished
jobs are paid into it from the vault (the Config account); earnings go back to the
wallet once their audits are past, and the collateral once the account's nodes have
all left (release); fraud burns it all (slash).

Off until SOLANA_PROGRAM is set in sinference/.env: nodes then serve w/o a stake
and nothing is paid.
"""

import base64
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from urllib.request import Request, urlopen

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

LAMPORTS = 10**9  # per SOL
FEES = 10_000      # lamports a wallet keeps for a stake's fee
DEVNET = "https://api.devnet.solana.com"
KEYPAIR = Path(__file__).resolve().parents[3] / "data/solana/authority.json"  # the coordinator's key
INCINERATOR = Pubkey.from_string("1nc1nerator11111111111111111111111111111111")  # slashed SOL is burned
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")

# Stake account: discriminator, wallet, account id, banned, bump
STAKE = struct.Struct("<8s32s16s?B")


class StakeError(Exception):
    pass


def discriminator(kind, name):
    """Anchor's: the first 8 bytes of sha256("global:pay"), sha256("account:Stake"), ..."""
    return hashlib.sha256(f"{kind}:{name}".encode()).digest()[:8]


def sol(lamports):
    return f"{lamports / LAMPORTS:g}"


class Stakes:
    def __init__(self, rpc, program, keypair, min_stake_usd, sol_usd):
        self.rpc = rpc
        self.min_stake_usd = min_stake_usd  # per serving node
        self.sol_usd = sol_usd              # → USD per SOL, or None (rates.sol_usd)
        self.program = Pubkey.from_string(program) if program else None
        if self.program:
            self.authority = Keypair.from_json(Path(keypair).read_text())
            self.config = Pubkey.find_program_address([b"config"], self.program)[0]

    @classmethod
    def from_env(cls, sol_usd):
        return cls(os.environ.get("SOLANA_RPC") or DEVNET, os.environ.get("SOLANA_PROGRAM"),
                   os.environ.get("SOLANA_KEYPAIR") or KEYPAIR, float(os.environ.get("MIN_STAKE_USD") or 20), sol_usd)

    @property
    def off(self):
        return self.program is None

    def call(self, method, *params):
        """Solana JSON-RPC"""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode()
        with urlopen(Request(self.rpc, body, {"Content-Type": "application/json"}), timeout=20) as response:
            reply = json.load(response)
        if "error" in reply:
            raise StakeError(reply["error"].get("message", "RPC error"))
        return reply["result"]

    def address(self, wallet, user_id):
        return Pubkey.find_program_address(
            [b"stake", bytes(Pubkey.from_string(wallet)), bytes.fromhex(user_id)], self.program)[0]


    #########
    # Reads #
    #########

    def read(self, wallet, user_id):
        """An account's stake, {free (lamports over its rent), banned}; None if it never staked."""
        info = self.call("getAccountInfo", str(self.address(wallet, user_id)),
                         {"encoding": "base64", "commitment": "confirmed"})["value"]
        if not info or info["owner"] != str(self.program):
            return None
        data = base64.b64decode(info["data"][0])
        if len(data) < STAKE.size or data[:8] != discriminator("account", "Stake"):
            return None

        _, _, _, banned, _ = STAKE.unpack_from(data)
        rent = self.call("getMinimumBalanceForRentExemption", len(data))
        return {"free": info["lamports"] - rent, "banned": banned}

    def cover(self, wallet, secret, user_id, nodes):
        """Make sure an account's stake covers `nodes` nodes, staking the rest from its wallet.

        None once it does, else why not (no wallet, not enough SOL, slashed, ...).
        """
        if self.off or not self.min_stake_usd:
            return None
        if not secret:
            return "Add a Solana wallet for your nodes, on your nodes page or w/ sinference up."
        rate = self.sol_usd()
        if not rate:
            return "Couldn't get SOL's price. Try again shortly."
        need = round(self.min_stake_usd * nodes / rate * LAMPORTS)

        try:
            stake = self.read(wallet, user_id)
            if stake and stake["banned"]:
                return "This stake was slashed after a failed audit."
            short = need - max(stake["free"], 0) if stake else need
            if short > 0:
                self.stake(secret, user_id, short, new=not stake)
        except (OSError, ValueError, StakeError) as exc:
            return str(exc)
        return None


    ##########
    # Writes #
    ##########

    def send(self, instructions, signer=None):
        """Sign + send (as the coordinator, unless told otherwise), then wait until it's confirmed: its signature."""
        signer = signer or self.authority
        blockhash = Hash.from_string(self.call("getLatestBlockhash", {"commitment": "confirmed"})["value"]["blockhash"])
        message = Message.new_with_blockhash(instructions, signer.pubkey(), blockhash)
        signature = self.call("sendTransaction", base64.b64encode(bytes(Transaction([signer], message, blockhash))).decode(),
                              {"encoding": "base64"})

        for _ in range(60):
            status = self.call("getSignatureStatuses", [signature])["value"][0]
            if status and status.get("err"):
                raise StakeError(f"transaction {signature} failed: {status['err']}")
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                return signature
            time.sleep(1)
        raise StakeError(f"transaction {signature} wasn't confirmed in time")

    def initialize(self):
        """Once, right after deploying: this key becomes the coordinator. None if it's already initialized."""
        if self.call("getAccountInfo", str(self.config), {"encoding": "base64"})["value"]:
            return None
        return self.send([Instruction(
            self.program, discriminator("global", "initialize"),
            [AccountMeta(self.authority.pubkey(), True, True), AccountMeta(self.config, False, True),
             AccountMeta(SYSTEM, False, False)])])

    def stake(self, secret, user_id, lamports, new):
        """From the account's wallet into its stake (made the first time), signed + paid by the wallet."""
        wallet = Keypair.from_base58_string(secret)
        rent = self.call("getMinimumBalanceForRentExemption", STAKE.size) if new else 0
        balance = self.call("getBalance", str(wallet.pubkey()), {"commitment": "confirmed"})["value"]
        if balance < lamports + rent + FEES:
            raise StakeError(f"Your wallet {wallet.pubkey()} needs {sol(lamports + rent + FEES - balance)} more SOL "
                             f"to stake for your nodes (devnet: faucet.solana.com).")

        return self.send([Instruction(
            self.program, discriminator("global", "stake") + bytes.fromhex(user_id) + struct.pack("<Q", lamports),
            [AccountMeta(wallet.pubkey(), True, True), AccountMeta(self.address(str(wallet.pubkey()), user_id), False, True),
             AccountMeta(SYSTEM, False, False)])], signer=wallet)

    def release(self, wallet, user_id, lamports):
        """From an account's stake back to its wallet (earnings past their audits, or collateral)."""
        return self.send([Instruction(
            self.program, discriminator("global", "release") + struct.pack("<Q", lamports),
            [AccountMeta(self.authority.pubkey(), True, False), AccountMeta(self.config, False, False),
             AccountMeta(self.address(wallet, user_id), False, True),
             AccountMeta(Pubkey.from_string(wallet), False, True)])])

    def pay(self, job, record, payees):
        """One transaction per job: each owner's share into their stake, w/ the job id + its record's hash."""
        shares = defaultdict(int)
        for payee in payees:
            shares[self.address(payee["wallet"], payee["user_id"])] += payee["lamports"]

        record = hashlib.sha256(record.encode()).digest()
        return self.send([Instruction(
            self.program, discriminator("global", "pay") + bytes.fromhex(job) + record + struct.pack("<Q", lamports),
            [AccountMeta(self.authority.pubkey(), True, False), AccountMeta(self.config, False, True),
             AccountMeta(stake, False, True)]) for stake, lamports in shares.items()])

    def slash(self, wallet, user_id, job, evidence):
        """Burn an account's whole stake and ban it, w/ the audited job's id + its evidence's hash."""
        if self.off:
            return None
        return self.send([Instruction(
            self.program, discriminator("global", "slash") + bytes.fromhex(job) + evidence,
            [AccountMeta(self.authority.pubkey(), True, False), AccountMeta(self.config, False, False),
             AccountMeta(self.address(wallet, user_id), False, True), AccountMeta(INCINERATOR, False, True)])])
