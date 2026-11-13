import NextAuth from "next-auth";
import { edgeAuthConfig } from "./edge-config";

export const { auth } = NextAuth(edgeAuthConfig);
